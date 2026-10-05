import asyncio
import json
from pathlib import Path
import shlex

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


def test_text_layer_is_bounded_and_not_counted_as_a_page_view(pages):
    service, run, context, command = pages
    attempt_id = context["attempt"].id
    first = command(["--json", "task", "page", attempt_id, "--number", "1", "--text"])
    assert "The result is clear." in first["text"]
    assert first["evidence"] is False and first["text_layer"] == "pdf" and first["next_command"] is None
    continuation = ["--offset", "4", "--text-digest", first["text_digest"]]
    middle = command(["--json", "task", "page", attempt_id, "--number", "1", "--text", *continuation])
    assert middle["text"] == first["text"][4:] and middle["text_digest"] == first["text_digest"]
    receipt = submit(
        service,
        context,
        {
            "summary": "Checked the page text layer only.",
            "findings": [],
            "scope": {
                "completion": "partial",
                "checked": [{"source_path": "manuscript.pdf", "page": 1}],
                "outstanding": [],
                "limitations": [],
            },
        },
    )
    assert receipt["attempt"].status.value == "completed"
    report = service.render_report(run.id, "json")
    assert report["review_tool_access"][0]["returns"]["page_text"] == 2
    assert report["review_coverage_audit"][0]["declared_without_task_page"] == [1]


def test_long_text_layer_continues_with_offsets(pages, monkeypatch):
    service, _, context, command = pages
    text = "页面文字 " * 3000
    monkeypatch.setattr("scriptorium.service.page_text", lambda pdf, page: text)
    collected = ""
    arguments = ["--json", "task", "page", context["attempt"].id, "--number", "1", "--text"]
    while arguments:
        response = command(arguments)
        assert response["offset"] == len(collected)
        collected += response["text"]
        arguments = shlex.split(response["next_command"])[1:] if response["next_command"] else None
    assert collected == text


def test_text_continuations_are_bound_to_one_extraction(pages, monkeypatch):
    service, run, context, _ = pages
    attempt_id = context["attempt"].id
    monkeypatch.setattr("scriptorium.service.page_text", lambda pdf, page: "first extraction " * 1000)
    first = service.page_task(attempt_id, 1, text=True)
    assert f"--text-digest {first['text_digest']}" in first["next_command"]
    with pytest.raises(ConfigurationError, match="requires --text-digest"):
        service.page_task(attempt_id, 1, text=True, offset=first["next_offset"])
    # A different extractor result must not be stitched onto fragments of the first one.
    monkeypatch.setattr("scriptorium.service.page_text", lambda pdf, page: "second extraction " * 1000)
    before = service.database.list_events(run.id)
    with pytest.raises(ConfigurationError, match="page text layer changed"):
        service.page_task(attempt_id, 1, text=True, offset=first["next_offset"], text_digest=first["text_digest"])
    assert service.database.list_events(run.id) == before


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
    views = service.repo / ".scriptorium/runs" / run.id / "page-views"
    kept = sum(path.stat().st_size for path in views.glob("*.png"))
    monkeypatch.setattr("scriptorium.service.MAX_RUN_PAGE_VIEW_BYTES", kept)
    with pytest.raises(StateError, match="reached the 0 MiB limit"):
        service.page_task(attempt_id, 1, scale=3.0)
    assert sum(path.stat().st_size for path in views.glob("*.png")) == kept
    assert service.page_task(attempt_id, 1, scale=2.0)["path"] == second["path"]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"scale": 5.0}, "scale"),
        ({"crop": (0.5, 0.0, 0.5, 1.0)}, "crop"),
        ({"text": True, "scale": 2.0}, "cannot be combined"),
        ({"offset": 3}, "require --text"),
        ({"text_digest": "a" * 64}, "require --text"),
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

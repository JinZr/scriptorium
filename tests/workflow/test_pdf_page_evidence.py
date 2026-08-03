import asyncio
from hashlib import sha256
import json
from pathlib import Path

import fitz
import pytest

from scriptorium.domain import AgentRole, RunStatus
from scriptorium.manuscript import BuildResult, ManuscriptManager
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

from ._support import FakeAgentRuntime, make_repository


class PagePdfManuscriptManager(ManuscriptManager):
    def __init__(self, repo, page_kind):
        super().__init__(repo)
        self.page_kind = page_kind

    def build(self, workspace, manuscript):
        pdf_path = workspace / Path(manuscript.main).with_suffix(".pdf")
        pdf_path.unlink(missing_ok=True)
        document = fitz.open()
        try:
            page = document.new_page(width=500, height=220)
            if self.page_kind == "vector":
                page.draw_rect(fitz.Rect(40, 40, 220, 160), color=(0, 0, 0), fill=(0.7, 0.8, 1))
                page.draw_line(fitz.Point(60, 140), fitz.Point(200, 60), color=(1, 0, 0), width=4)
            elif self.page_kind != "empty":
                image_document = fitz.open()
                try:
                    image_page = image_document.new_page(width=420, height=100)
                    text = {
                        "raster": "Rendered image text",
                        "ligature": "The fi ligature is visible",
                        "unicode_minus": "Effect size: -0.42",
                        "line_break": "cross-line hyphenation",
                        "stale_hidden": "Current visible result",
                    }[self.page_kind]
                    image_page.insert_text((20, 55), text, fontsize=18)
                    pixmap = image_page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                finally:
                    image_document.close()
                page.insert_image(fitz.Rect(20, 20, 480, 140), pixmap=pixmap)
                if self.page_kind == "stale_hidden":
                    page.insert_text((20, 200), "Outdated native text", fontsize=8)
            document.save(pdf_path)
        finally:
            document.close()
        return BuildResult(pdf_path=pdf_path, log="fake page PDF build succeeded")


class PageEvidenceRuntime(FakeAgentRuntime):
    @staticmethod
    def _review_output(role, workspace):
        output = FakeAgentRuntime._review_output(role, workspace)
        if role == AgentRole.SUBSTANTIVE_REVIEW:
            output["findings"][0]["evidence"] = [{"source_path": "manuscript.pdf", "page": 1}]
        return output


@pytest.mark.parametrize(
    "page_kind",
    ["raster", "vector", "empty", "ligature", "unicode_minus", "line_break", "stale_hidden"],
)
def test_pdf_page_anchor_does_not_depend_on_any_text_layer(tmp_path, page_kind):
    repo = make_repository(tmp_path)
    runtime = PageEvidenceRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PagePdfManuscriptManager(repo, page_kind),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.AWAITING_DECISION
        assert all("transcription" not in item["task"].stage for item in started["tasks"])
        assert runtime.run_calls[AgentRole.VISUAL_TRANSCRIPTION] == 0
        assert service.list_findings(started["run"].id)[0].evidence == ({"source_path": "manuscript.pdf", "page": 1},)


def test_pdf_page_anchor_is_bound_to_the_attempt_bundle_and_page_digest(tmp_path):
    repo = make_repository(tmp_path)
    runtime = PageEvidenceRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PagePdfManuscriptManager(repo, "raster"),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        substantive = next(item for item in started["tasks"] if item["task"].role == AgentRole.SUBSTANTIVE_REVIEW)
        attempt = substantive["attempts"][-1]
        workspace = runtime.workspaces[AgentRole.SUBSTANTIVE_REVIEW]
        source_map = json.loads((workspace / "source-map.json").read_text(encoding="utf-8"))
        page_path = workspace / source_map["compiled_pdf"]["pages"][0]["read_path"]

        assert attempt.bundle_digest == Armarius._directory_digest(workspace)
        assert source_map["compiled_pdf"]["pages"][0]["page_digest"] == sha256(page_path.read_bytes()).hexdigest()

import asyncio
import json
from pathlib import Path
import re

import fitz
import pytest

from scriptorium.domain import AgentRole, AttemptStatus, Run, RunStatus, TaskStatus
from scriptorium.errors import InfrastructureError
from scriptorium.manuscript import BuildResult, ManuscriptManager
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

from ._support import FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


class RasterPdfManuscriptManager(ManuscriptManager):
    def __init__(self, repo, *, native_text=False):
        super().__init__(repo)
        self.native_text = native_text

    def build(self, workspace, manuscript):
        sentence = (workspace / manuscript.main).read_text(encoding="utf-8").splitlines()[2]
        image_document = fitz.open()
        try:
            image_page = image_document.new_page(width=420, height=80)
            image_page.insert_text((20, 45), sentence, fontsize=18)
            pixmap = image_page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        finally:
            image_document.close()
        pdf_path = workspace / Path(manuscript.main).with_suffix(".pdf")
        pdf_path.unlink(missing_ok=True)
        document = fitz.open()
        try:
            page = document.new_page(width=500, height=200 if self.native_text else 140)
            page.insert_image(fitz.Rect(20, 20, 480, 120), pixmap=pixmap)
            if self.native_text:
                page.insert_text((20, 170), sentence, fontsize=11)
            document.save(pdf_path)
        finally:
            document.close()
        return BuildResult(pdf_path=pdf_path, log="fake raster PDF build succeeded")


class VisualRuntime(FakeAgentRuntime):
    def __init__(
        self,
        *,
        interrupt_visual_once=False,
        interrupt_copyedit_once=False,
        invalid_visual=None,
        verification_issue=False,
        visual_text=None,
    ):
        super().__init__(interrupt_copyedit_once=interrupt_copyedit_once)
        self.interrupt_visual_once = interrupt_visual_once
        self.invalid_visual = invalid_visual
        self.verification_issue = verification_issue
        self.visual_text = visual_text
        self.call_order = []

    async def run_agent(self, task, role, workspace, schema, session_dir):
        self.call_order.append(role)
        if role != AgentRole.VISUAL_TRANSCRIPTION:
            return await super().run_agent(task, role, workspace, schema, session_dir)
        self.run_calls[role] += 1
        self.tasks[role] = task
        self.workspaces[role] = workspace
        self.session_dirs[role] = session_dir
        self.session_dir_calls.append((role, session_dir))
        ordinal = self.run_calls[role]
        if self.interrupt_visual_once and ordinal == 1:
            return self._result(role, ordinal, "interrupted", None)
        return self._result(role, ordinal, "completed", self._visual_output(task, workspace))

    async def resume_agent(self, thread_id, task, role, workspace, schema, session_dir):
        self.call_order.append(role)
        if role != AgentRole.VISUAL_TRANSCRIPTION:
            return await super().resume_agent(thread_id, task, role, workspace, schema, session_dir)
        self.resume_calls.append(role)
        self.tasks[role] = task
        assert session_dir == self.session_dirs[role]
        self.session_dir_calls.append((role, session_dir))
        return self._result(role, 2, "completed", self._visual_output(task, workspace))

    def _review_output(self, role, workspace):
        output = super()._review_output(role, workspace)
        if role == AgentRole.SUBSTANTIVE_REVIEW:
            sentence = (workspace / "sources" / "main.tex").read_text(encoding="utf-8").splitlines()[2]
            output["findings"][0]["evidence"] = [
                {
                    "source_path": "manuscript.pdf",
                    "page": 1,
                    "quoted_text": sentence,
                }
            ]
        return output

    def _verification_output(self, task):
        if not self.verification_issue:
            return super()._verification_output(task)
        finding_ids = re.findall(r'"id": "(finding_[^"]+)"', task)
        return {
            "verdict": "fail",
            "summary": "The changed raster wording needs human review.",
            "resolved_finding_ids": finding_ids,
            "issues": [
                {
                    "title": "Raster wording changed",
                    "explanation": "The patched page contains the changed wording.",
                    "evidence": [
                        {
                            "source_path": "manuscript.pdf",
                            "page": 1,
                            "quoted_text": "The result is clear.",
                        }
                    ],
                }
            ],
        }

    def _visual_output(self, task, workspace):
        original_task = (workspace / "task.md").read_text(encoding="utf-8")
        prompt_request = json.loads(original_task.split("Requested pages:\n", 1)[1].split("\n\n", 1)[0])
        request = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        assert request == prompt_request
        default_text = (
            "The result is clear." if request["stage"] == "verification_transcription" else "The result is teh clear."
        )
        text = default_text if self.visual_text is None else self.visual_text
        output = {
            "pdf_digest": request["pdf_digest"],
            "pages": [
                {
                    "page": page["page"],
                    "page_digest": page["page_digest"],
                    "text": text,
                }
                for page in request["pages"]
            ],
        }
        if self.invalid_visual == "pdf_digest":
            output["pdf_digest"] = "0" * 64
        elif self.invalid_visual == "page_digest":
            output["pages"][0]["page_digest"] = "0" * 64
        elif self.invalid_visual == "duplicate":
            output["pages"].append(dict(output["pages"][0]))
        elif self.invalid_visual == "missing":
            output["pages"] = []
        elif self.invalid_visual == "extra":
            output["pages"].append(
                {
                    "page": 2,
                    "page_digest": output["pages"][0]["page_digest"],
                    "text": "extra",
                }
            )
        return output


def test_raster_pdf_quote_uses_transcription_before_reviewers(tmp_path):
    repo = make_repository(tmp_path)
    runtime = VisualRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=RasterPdfManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.AWAITING_DECISION
        assert runtime.call_order[0] == AgentRole.VISUAL_TRANSCRIPTION
        transcription_tasks = [item for item in started["tasks"] if item["task"].stage == "review_transcription"]
        assert len(transcription_tasks) == 1
        assert transcription_tasks[0]["task"].status == TaskStatus.COMPLETED
        assert service.list_findings(started["run"].id)[0].evidence[0]["quoted_text"] == ("The result is teh clear.")
        visual_workspace = runtime.workspaces[AgentRole.VISUAL_TRANSCRIPTION]
        assert not (visual_workspace / "sources").exists()
        assert not (visual_workspace / "manuscript.pdf").exists()
        assert sorted(path.relative_to(visual_workspace).as_posix() for path in visual_workspace.rglob("*")) == [
            "manifest.json",
            "pages",
            "pages/page-0001.png",
            "task.md",
        ]
        review_workspace = runtime.workspaces[AgentRole.SUBSTANTIVE_REVIEW]
        assert not any("transcription" in path.name for path in review_workspace.rglob("*"))


def test_text_only_pdf_does_not_create_visual_transcription_task(tmp_path):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert all(item["task"].stage != "review_transcription" for item in started["tasks"])
        assert runtime.run_calls[AgentRole.VISUAL_TRANSCRIPTION] == 0


def test_raster_pdf_quote_absent_from_valid_transcription_is_rejected(tmp_path):
    repo = make_repository(tmp_path)
    runtime = VisualRuntime(visual_text="Different visible text.")

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=RasterPdfManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.REVIEWING
        substantive_task = next(item for item in started["tasks"] if item["task"].role == AgentRole.SUBSTANTIVE_REVIEW)
        assert [attempt.status for attempt in substantive_task["attempts"]] == [
            AttemptStatus.FAILED,
            AttemptStatus.FAILED,
        ]
        assert all(
            "quoted PDF evidence does not match manuscript.pdf page 1" in (attempt.error or "")
            for attempt in substantive_task["attempts"]
        )


def test_native_pdf_text_still_validates_when_raster_transcription_is_empty(tmp_path):
    repo = make_repository(tmp_path)
    runtime = VisualRuntime(visual_text="")

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=RasterPdfManuscriptManager(repo, native_text=True),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.AWAITING_DECISION
        assert runtime.call_order[0] == AgentRole.VISUAL_TRANSCRIPTION
        assert service.list_findings(started["run"].id)[0].evidence[0]["quoted_text"] == ("The result is teh clear.")


def test_frozen_visual_transcription_contract_is_all_or_none():
    common = {
        "repository": "repo",
        "commit_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "profile": "quick",
        "config_digest": "c" * 64,
    }

    assert Armarius._has_visual_transcription_contract(Run(**common)) is False

    partial = Run(
        **common,
        frozen_config={"role_routes": {AgentRole.VISUAL_TRANSCRIPTION.value: "primary"}},
    )
    with pytest.raises(InfrastructureError, match="frozen visual transcription contract is incomplete"):
        Armarius._has_visual_transcription_contract(partial)


@pytest.mark.parametrize("invalid_visual", ["pdf_digest", "page_digest", "duplicate", "missing", "extra"])
def test_invalid_visual_transcription_blocks_reviewers(tmp_path, invalid_visual):
    repo = make_repository(tmp_path)
    runtime = VisualRuntime(invalid_visual=invalid_visual)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=RasterPdfManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))

        assert started["run"].status == RunStatus.REVIEWING
        assert len(started["tasks"]) == 1
        task = started["tasks"][0]
        assert task["task"].stage == "review_transcription"
        assert [attempt.status for attempt in task["attempts"]] == [
            AttemptStatus.FAILED,
            AttemptStatus.FAILED,
        ]
        assert all(role == AgentRole.VISUAL_TRANSCRIPTION for role in runtime.call_order)


def test_interrupted_visual_transcription_resumes_before_reviewers(tmp_path):
    repo = make_repository(tmp_path)
    runtime = VisualRuntime(interrupt_visual_once=True)
    manager = RasterPdfManuscriptManager(repo)

    with ScriptoriumService(repo, runtime_factory=lambda route: runtime, manuscript_manager=manager) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
        assert started["run"].status == RunStatus.REVIEWING
        assert len(started["tasks"]) == 1

    with ScriptoriumService(repo, runtime_factory=lambda route: runtime, manuscript_manager=manager) as service:
        resumed = asyncio.run(service.resume_run(run_id))

        assert resumed["run"].status == RunStatus.AWAITING_DECISION
        assert runtime.resume_calls == [AgentRole.VISUAL_TRANSCRIPTION]
        assert runtime.call_order[:2] == [
            AgentRole.VISUAL_TRANSCRIPTION,
            AgentRole.VISUAL_TRANSCRIPTION,
        ]


def test_completed_visual_transcription_is_reused_when_a_reviewer_resumes(tmp_path):
    repo = make_repository(tmp_path)
    runtime = VisualRuntime(interrupt_copyedit_once=True)
    manager = RasterPdfManuscriptManager(repo)

    with ScriptoriumService(repo, runtime_factory=lambda route: runtime, manuscript_manager=manager) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
        assert started["run"].status == RunStatus.REVIEWING
        assert runtime.run_calls[AgentRole.VISUAL_TRANSCRIPTION] == 1

    with ScriptoriumService(repo, runtime_factory=lambda route: runtime, manuscript_manager=manager) as service:
        resumed = asyncio.run(service.resume_run(run_id))

        assert resumed["run"].status == RunStatus.AWAITING_DECISION
        assert runtime.run_calls[AgentRole.VISUAL_TRANSCRIPTION] == 1
        assert AgentRole.VISUAL_TRANSCRIPTION not in runtime.resume_calls
        assert runtime.resume_calls == [AgentRole.COPYEDIT]


def test_different_patch_ids_do_not_reuse_identical_rendered_transcription(tmp_path):
    repo = make_repository(tmp_path)
    runtime = VisualRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=RasterPdfManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run = started["run"]
        bundle = service.armarius._bundle_for_run(run)
        pages = service.armarius._visual_page_records(bundle)

        first = asyncio.run(
            service.armarius._run_visual_transcription(
                run,
                "verification_transcription",
                bundle,
                pages,
                bundle_id="patch-one",
            )
        )
        second = asyncio.run(
            service.armarius._run_visual_transcription(
                run,
                "verification_transcription",
                bundle,
                pages,
                bundle_id="patch-two",
            )
        )

        assert first is not None
        assert second is not None
        verification_tasks = [
            task for task in service.database.list_tasks(run.id) if task.stage == "verification_transcription"
        ]
        assert len(verification_tasks) == 2
        assert len({task.input_digest for task in verification_tasks}) == 2


def test_visual_transcription_budget_gate_precedes_review_tasks(tmp_path):
    repo = make_repository(tmp_path)
    local_config = repo / ".scriptorium" / "config.toml"
    local_config.write_text(
        local_config.read_text(encoding="utf-8")
        .replace('model_provider = "ollama"', 'model_provider = "openai"')
        .replace("input_usd_per_million = 0", "input_usd_per_million = 1")
        .replace("output_usd_per_million = 0", "output_usd_per_million = 1"),
        encoding="utf-8",
    )
    runtime = VisualRuntime()

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=RasterPdfManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", 0))

        assert started["run"].status == RunStatus.WAITING_BUDGET
        assert len(started["tasks"]) == 1
        assert started["tasks"][0]["task"].stage == "review_transcription"
        assert started["tasks"][0]["attempts"] == []
        assert not runtime.call_order


def test_visual_transcription_retry_on_another_route_starts_new_session(tmp_path):
    repo = make_repository(tmp_path)
    local_config = repo / ".scriptorium" / "config.toml"
    local_config.write_text(
        local_config.read_text(encoding="utf-8")
        + (
            "\n[routes.alternate]\n"
            'runtime = "codex"\n'
            'model_provider = "ollama"\n'
            'model = "fake-model"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
        ),
        encoding="utf-8",
    )
    runtime = VisualRuntime(interrupt_visual_once=True)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=RasterPdfManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        task = started["tasks"][0]["task"]

        retried = asyncio.run(service.retry_task(started["run"].id, task.id, "alternate"))

        assert retried["run"].status == RunStatus.AWAITING_DECISION
        assert runtime.run_calls[AgentRole.VISUAL_TRANSCRIPTION] == 2
        assert AgentRole.VISUAL_TRANSCRIPTION not in runtime.resume_calls
        transcription_tasks = [
            item["task"] for item in retried["tasks"] if item["task"].stage == "review_transcription"
        ]
        assert {item.route for item in transcription_tasks} == {"primary", "alternate"}


def test_patched_bundle_gets_its_own_transcription_before_verification(tmp_path):
    repo = make_repository(tmp_path)
    runtime = VisualRuntime(verification_issue=True)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=RasterPdfManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        run_id = started["run"].id
        finding = service.list_findings(run_id)[0]
        service.decide_finding(finding.id, "confirm", "Correct the typo.")
        revised = asyncio.run(service.resume_run(run_id))
        patch_id = revised["patch_ids"][0]
        service.decide_patch(patch_id, "approve", "Verify the patch.")

        verified = asyncio.run(service.resume_run(run_id))

        assert verified["run"].status == RunStatus.AWAITING_PATCH_APPROVAL
        visual_positions = [
            index for index, role in enumerate(runtime.call_order) if role == AgentRole.VISUAL_TRANSCRIPTION
        ]
        assert len(visual_positions) == 2
        assert visual_positions[1] < runtime.call_order.index(AgentRole.VERIFICATION)
        tasks = service.database.list_tasks(run_id)
        transcription_tasks = [task for task in tasks if task.role == AgentRole.VISUAL_TRANSCRIPTION]
        assert {task.stage for task in transcription_tasks} == {
            "review_transcription",
            "verification_transcription",
        }
        outputs = []
        for task in transcription_tasks:
            attempt = service.database.list_attempts(task.id)[-1]
            outputs.append(json.loads(service.artifacts.get_bytes(attempt.output_artifact_digest).decode("utf-8")))
        assert outputs[0]["pdf_digest"] != outputs[1]["pdf_digest"]
        assert {output["pages"][0]["text"] for output in outputs} == {
            "The result is teh clear.",
            "The result is clear.",
        }

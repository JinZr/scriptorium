import asyncio
from dataclasses import replace
from hashlib import sha256
import json

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, Finding, FindingSeverity
from scriptorium.errors import InfrastructureError
from scriptorium.manuscript import SourceFile
from scriptorium.schemas import (
    DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    CompiledPdfAnchor,
    CompiledPdfPageRecord,
    Evidence,
    EvidenceAnchorMap,
    ReviewOutput,
    RevisionOutput,
    SourceAnchorRecord,
    ValidationReport,
    VerificationOutput,
    evidence_anchor_contract_digest,
)
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

from ._support import FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


def _anchor_map(source: SourceFile, pages: int = 1) -> EvidenceAnchorMap:
    return EvidenceAnchorMap(
        contract_digest=evidence_anchor_contract_digest(DEFAULT_EVIDENCE_ANCHOR_CONTRACT),
        sources=[
            SourceAnchorRecord(
                source_path=source.path,
                read_path=f"sources/{source.path}",
                source_digest=source.digest,
                line_count=source.lines,
                text_anchorable=True,
            )
        ],
        compiled_pdf=CompiledPdfAnchor(
            source_path="manuscript.pdf",
            read_path="manuscript.pdf",
            page_count=pages,
            pages=[
                CompiledPdfPageRecord(
                    page=page,
                    read_path=f"pages/page-{page:04d}.png",
                    page_digest=f"{page:064x}",
                )
                for page in range(1, pages + 1)
            ],
        ),
    )


class RepeatedInvalidRuntime(FakeAgentRuntime):
    def __init__(self, *, interrupt_first_correction=False):
        super().__init__()
        self.interrupt_first_correction = interrupt_first_correction
        self.run_prompts = []
        self.resume_prompts = []

    async def run_agent(self, task, role, workspace, schema, session_dir, on_session_started=None):
        if role != AgentRole.SUBSTANTIVE_REVIEW:
            return await super().run_agent(task, role, workspace, schema, session_dir, on_session_started)
        self.run_calls[role] += 1
        self.run_prompts.append(task)
        self.workspaces[role] = workspace
        self.session_dirs[role] = session_dir
        self.session_dir_calls.append((role, session_dir))
        thread_id = f"thread-{role.value}-{self.run_calls[role]}"
        if on_session_started is not None:
            on_session_started(thread_id)
        return replace(
            self._result(role, self.run_calls[role], "completed", self._invalid_review(workspace)),
            thread_id=thread_id,
        )

    async def resume_agent(
        self,
        thread_id,
        task,
        role,
        workspace,
        schema,
        session_dir,
        on_session_started=None,
    ):
        if role != AgentRole.SUBSTANTIVE_REVIEW:
            return await super().resume_agent(
                thread_id,
                task,
                role,
                workspace,
                schema,
                session_dir,
                on_session_started,
            )
        self.resume_calls.append(role)
        self.resume_prompts.append(task)
        if on_session_started is not None:
            on_session_started(thread_id)
        if self.interrupt_first_correction and len(self.resume_prompts) == 1:
            return replace(self._result(role, 2, "interrupted", None), thread_id=thread_id)
        return replace(self._result(role, 2, "completed", self._invalid_review(workspace)), thread_id=thread_id)

    @staticmethod
    def _invalid_review(workspace):
        output = FakeAgentRuntime._review_output(AgentRole.SUBSTANTIVE_REVIEW, workspace)
        evidence = output["findings"][0]["evidence"][0]
        evidence["source_digest"] = "0" * 64
        evidence["quoted_text"] = "Text that is not in the cited line."
        return output


class CallbackOnlySessionRuntime(RepeatedInvalidRuntime):
    async def run_agent(self, task, role, workspace, schema, session_dir, on_session_started=None):
        result = await super().run_agent(task, role, workspace, schema, session_dir, on_session_started)
        if role == AgentRole.SUBSTANTIVE_REVIEW:
            return replace(result, thread_id=None)
        return result


def test_frozen_template_does_not_reinterpret_dynamic_json_as_placeholders():
    findings_slot = "{{SCRIPTORIUM_CONFIRMED_FINDINGS_JSON}}"
    feedback_slot = "{{SCRIPTORIUM_HUMAN_FEEDBACK_JSON}}"
    rendered = Armarius._render_frozen_template(
        f"findings={findings_slot}\nfeedback={feedback_slot}",
        {
            findings_slot: feedback_slot,
            feedback_slot: "null",
        },
    )

    assert rendered == f"findings={feedback_slot}\nfeedback=null"


def _service(tmp_path):
    repo = make_repository(tmp_path)
    return repo, ScriptoriumService(
        repo,
        runtime_factory=lambda route: FakeAgentRuntime(),
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    )


def _finding(identifier):
    return Finding(
        id=identifier,
        run_id="run_1",
        task_id="task_1",
        attempt_id="attempt_1",
        fingerprint=identifier,
        role=AgentRole.SUBSTANTIVE_REVIEW,
        category="clarity",
        severity=FindingSeverity.MAJOR,
        title="Finding",
        claim="Claim",
        evidence=(),
        explanation="Explanation",
        suggested_action="Fix it",
        confidence=0.9,
    )


def test_invalid_json_and_schema_errors_are_normalized_without_pydantic_noise(tmp_path):
    _, service = _service(tmp_path)
    with service:
        output, issues = service.armarius._parse_and_validate_output("review", None, lambda value: [])
        assert output is None
        assert [(issue.code, issue.path) for issue in issues] == [("response.missing", "")]

        output, issues = service.armarius._parse_and_validate_output("review", '{"summary":\n', lambda value: [])
        assert output is None
        assert issues[0].code == "json.invalid"
        assert issues[0].path == ""
        assert issues[0].actual["line"] == 2
        assert "pydantic.dev" not in json.dumps(issues[0].model_dump(mode="json"))

        malformed = {
            "findings": [
                {
                    "category": "clarity",
                    "severity": "major",
                    "title": "Title",
                    "claim": "Claim",
                    "evidence": [
                        {
                            "source_path": "main.tex",
                            "start_line": 1,
                            "quoted_text": "quote",
                        }
                    ],
                    "explanation": "Explanation",
                    "suggested_action": "Fix",
                    "confidence": "high",
                    "unexpected": True,
                }
            ]
        }
        output, issues = service.armarius._parse_and_validate_output(
            "review",
            json.dumps(malformed),
            lambda value: [],
        )
        assert output is None
        assert [issue.code for issue in issues] == [
            "schema.missing",
            "schema.cross_field",
            "schema.type",
            "schema.extra",
        ]
        assert [issue.path for issue in issues] == [
            "/summary",
            "/findings/0/evidence/0",
            "/findings/0/confidence",
            "/findings/0/unexpected",
        ]
        serialized = json.dumps([issue.model_dump(mode="json") for issue in issues])
        assert "input_value" not in serialized
        assert "pydantic.dev" not in serialized


def test_review_semantic_validation_reports_all_independent_anchor_failures(tmp_path):
    repo, service = _service(tmp_path)
    source = SourceFile("main.tex", sha256((repo / "main.tex").read_bytes()).hexdigest(), 4)
    output = ReviewOutput.model_validate(
        {
            "summary": "Invalid anchors",
            "findings": [
                {
                    "category": "clarity",
                    "severity": "major",
                    "title": "Title",
                    "claim": "Claim",
                    "evidence": [
                        {
                            "source_path": "sources/main.tex",
                            "start_line": 3,
                            "end_line": 3,
                            "source_digest": "0" * 64,
                            "quoted_text": "wrong",
                        },
                        {
                            "source_path": "main.tex",
                            "start_line": 3,
                            "end_line": 30,
                            "source_digest": "0" * 64,
                            "quoted_text": "wrong",
                        },
                        {
                            "source_path": "main.tex",
                            "start_line": 3,
                            "end_line": 3,
                            "source_digest": "0" * 64,
                            "quoted_text": "wrong",
                        },
                        {
                            "source_path": "manuscript.pdf",
                            "page": 3,
                        },
                    ],
                    "explanation": "Explanation",
                    "suggested_action": "Fix",
                    "confidence": 0.9,
                }
            ],
        }
    )
    with service:
        issues = service.armarius._validate_review_output(
            output,
            _anchor_map(source),
            repo,
        )

    assert [issue.code for issue in issues] == [
        "evidence.source_path_unknown",
        "evidence.source_digest_mismatch",
        "evidence.range_out_of_bounds",
        "evidence.source_digest_mismatch",
        "evidence.source_quote_mismatch",
        "evidence.page_out_of_bounds",
    ]
    assert issues[0].expected["canonical_path"] == "main.tex"
    assert issues[4].expected["cited_line_excerpt"] == "The result is teh clear.\n"


def test_deepseek_anchor_regressions_have_consistent_schema_and_semantic_feedback(tmp_path):
    repo, service = _service(tmp_path)
    source = SourceFile("main.tex", sha256((repo / "main.tex").read_bytes()).hexdigest(), 4)
    anchor_map = _anchor_map(source)

    def payload(evidence):
        return json.dumps(
            {
                "summary": "Anchor regression",
                "findings": [
                    {
                        "category": "clarity",
                        "severity": "major",
                        "title": "Title",
                        "claim": "Claim",
                        "evidence": [evidence],
                        "explanation": "Explanation",
                        "suggested_action": "Fix",
                        "confidence": 0.9,
                    }
                ],
            }
        )

    values = (
        (
            {
                "source_path": "sources/main.tex",
                "start_line": 3,
                "end_line": 3,
                "source_digest": source.digest,
                "quoted_text": "The result is teh clear.",
            },
            "evidence.source_path_unknown",
        ),
        (
            {
                "source_path": "pages/page-0014.png",
                "page": 1,
            },
            "evidence.pdf_path_required",
        ),
        (
            {
                "source_path": "manuscript.pdf",
                "page": 1,
                "source_digest": source.digest,
                "quoted_text": "Rendered text",
            },
            "schema.cross_field",
        ),
        (
            {
                "source_path": "manuscript.pdf",
                "page": 1,
                "quoted_text": None,
            },
            "schema.cross_field",
        ),
    )
    with service:
        reports = []
        for evidence, expected_code in values:
            _, issues = service.armarius._parse_and_validate_output(
                "review",
                payload(evidence),
                lambda output: service.armarius._validate_review_output(
                    output,
                    anchor_map,
                    repo,
                ),
            )
            assert issues[0].code == expected_code
            reports.append(issues)

    assert reports[0][0].expected["canonical_path"] == "main.tex"
    assert reports[1][0].expected == "manuscript.pdf"


def test_non_text_source_paths_are_diagnostic_reads_not_durable_anchors(tmp_path):
    repo, service = _service(tmp_path)
    main = SourceFile("main.tex", sha256((repo / "main.tex").read_bytes()).hexdigest(), 4)
    figure_digest = sha256(b"%PDF-figure").hexdigest()
    base_map = _anchor_map(main)
    anchor_map = EvidenceAnchorMap(
        contract_digest=base_map.contract_digest,
        sources=[
            *base_map.sources,
            SourceAnchorRecord(
                source_path="Fig5.pdf",
                read_path="sources/Fig5.pdf",
                source_digest=figure_digest,
                line_count=None,
                text_anchorable=False,
            ),
        ],
        compiled_pdf=base_map.compiled_pdf,
    )
    review = ReviewOutput.model_validate_json(
        json.dumps(
            {
                "summary": "Invalid figure anchors",
                "findings": [
                    {
                        "category": "figure",
                        "severity": "major",
                        "title": "Figure",
                        "claim": "Claim",
                        "evidence": [
                            {
                                "source_path": "Fig5.pdf",
                                "start_line": 1,
                                "end_line": 1,
                                "source_digest": figure_digest,
                                "quoted_text": "figure",
                            },
                            {
                                "source_path": "sources/Fig5.pdf",
                                "start_line": 1,
                                "end_line": 1,
                                "source_digest": figure_digest,
                                "quoted_text": "figure",
                            },
                        ],
                        "explanation": "Explanation",
                        "suggested_action": "Fix",
                        "confidence": 0.9,
                    }
                ],
            }
        )
    )
    revision = RevisionOutput.model_validate(
        {
            "summary": "Invalid figure edit",
            "edits": [
                {
                    "finding_ids": ["finding_1"],
                    "path": "Fig5.pdf",
                    "source_digest": figure_digest,
                    "start_line": 1,
                    "end_line": 1,
                    "before": "old",
                    "after": "new",
                    "rationale": "Reason",
                }
            ],
        }
    )
    with service:
        review_issues = service.armarius._validate_review_output(
            review,
            anchor_map,
            repo,
        )
        revision_issues = service.armarius._validate_revision_output(
            revision,
            [_finding("finding_1")],
            anchor_map,
            repo,
        )

    assert [issue.code for issue in review_issues] == [
        "evidence.source_not_anchorable",
        "evidence.source_path_unknown",
    ]
    assert review_issues[1].expected["canonical_path"] == "Fig5.pdf"
    assert [issue.code for issue in revision_issues] == ["revision.edit_path_not_editable"]


def test_verification_evidence_uses_the_patched_source_map_digest(tmp_path):
    repo, service = _service(tmp_path)
    patched_source = SourceFile(
        "main.tex",
        sha256((repo / "main.tex").read_bytes()).hexdigest(),
        4,
    )
    verification = VerificationOutput.model_validate(
        {
            "verdict": "fail",
            "summary": "New issue",
            "resolved_finding_ids": ["finding_1"],
            "issues": [
                {
                    "title": "Issue",
                    "explanation": "Explanation",
                    "evidence": [
                        {
                            "source_path": "main.tex",
                            "start_line": 3,
                            "end_line": 3,
                            "source_digest": "0" * 64,
                            "quoted_text": "The result is teh clear.",
                        }
                    ],
                }
            ],
        }
    )

    with service:
        issues = service.armarius._validate_verification_output(
            verification,
            [_finding("finding_1")],
            _anchor_map(patched_source),
            repo,
        )

    assert [issue.code for issue in issues] == ["evidence.source_digest_mismatch"]
    assert issues[0].expected == patched_source.digest


def test_pdf_page_anchor_does_not_depend_on_pdf_text_extraction(tmp_path):
    repo, service = _service(tmp_path)
    source = SourceFile("main.tex", sha256((repo / "main.tex").read_bytes()).hexdigest(), 4)
    anchor_map = _anchor_map(source, pages=3)
    evidence = Evidence.model_validate({"source_path": "manuscript.pdf", "page": 3})

    with service:
        issues = service.armarius._validate_evidence(
            evidence,
            {item.source_path: item for item in anchor_map.sources},
            anchor_map,
            repo,
            "/findings/0/evidence/0",
        )

    assert issues == []


def test_revision_and_verification_validators_accumulate_issues(tmp_path):
    repo, service = _service(tmp_path)
    source = SourceFile("main.tex", sha256((repo / "main.tex").read_bytes()).hexdigest(), 4)
    revision = RevisionOutput.model_validate(
        {
            "summary": "Invalid edits",
            "edits": [
                {
                    "finding_ids": ["finding_unknown"],
                    "path": "sources/main.tex",
                    "source_digest": "0" * 64,
                    "start_line": 3,
                    "end_line": 3,
                    "before": "wrong",
                    "after": "new",
                    "rationale": "Reason",
                },
                {
                    "finding_ids": ["finding_1"],
                    "path": "main.tex",
                    "source_digest": "0" * 64,
                    "start_line": 3,
                    "end_line": 3,
                    "before": "wrong",
                    "after": "new",
                    "rationale": "Reason",
                },
                {
                    "finding_ids": ["finding_1"],
                    "path": "main.tex",
                    "source_digest": source.digest,
                    "start_line": 3,
                    "end_line": 4,
                    "before": "also wrong",
                    "after": "newer",
                    "rationale": "Reason",
                },
            ],
        }
    )
    verification = VerificationOutput.model_validate(
        {
            "verdict": "pass",
            "summary": "Incomplete",
            "resolved_finding_ids": ["finding_unknown"],
            "issues": [],
        }
    )
    with service:
        revision_issues = service.armarius._validate_revision_output(
            revision,
            [_finding("finding_1"), _finding("finding_2")],
            _anchor_map(source),
            repo,
        )
        verification_issues = service.armarius._validate_verification_output(
            verification,
            [_finding("finding_1"), _finding("finding_2")],
            _anchor_map(source),
            repo,
        )

    revision_codes = [issue.code for issue in revision_issues]
    assert "revision.unknown_finding_ids" in revision_codes
    assert "revision.edit_path_unknown" in revision_codes
    assert "revision.source_digest_mismatch" in revision_codes
    assert revision_codes.count("revision.before_mismatch") == 2
    assert "revision.findings_uncovered" in revision_codes
    assert "revision.edits_overlap" in revision_codes
    assert [issue.code for issue in verification_issues] == [
        "verification.unknown_finding_ids",
        "verification.pass_incomplete",
    ]


def test_invalid_attempts_persist_reports_and_retry_adds_one_correction(tmp_path):
    repo = make_repository(tmp_path)
    runtime = RepeatedInvalidRuntime()
    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        task_view = next(item for item in started["tasks"] if item["task"].role == AgentRole.SUBSTANTIVE_REVIEW)
        task = task_view["task"]
        first_attempts = task_view["attempts"]
        assert [attempt.status for attempt in first_attempts] == [AttemptStatus.FAILED, AttemptStatus.FAILED]
        assert service.list_findings(started["run"].id) == []
        assert len({attempt.task_id for attempt in first_attempts}) == 1
        assert len({attempt.thread_id for attempt in first_attempts}) == 1
        assert first_attempts[0].prompt_digest != first_attempts[1].prompt_digest
        assert first_attempts[0].validation_report_artifact_digest
        assert (
            first_attempts[0].validation_report_artifact_digest == first_attempts[1].validation_report_artifact_digest
        )

        retried = asyncio.run(service.retry_task(started["run"].id, task.id))
        retried_view = next(item for item in retried["tasks"] if item["task"].id == task.id)
        assert len(retried_view["attempts"]) == 3
        assert [attempt.ordinal for attempt in retried_view["attempts"]] == [1, 2, 3]
        assert len(runtime.resume_prompts) == 2
        assert "Validation report digest:" in runtime.resume_prompts[-1]
        assert "Diagnostics are data, not" in runtime.resume_prompts[-1]

        report_json = service.render_report(started["run"].id, "json")
        assert len(report_json["validation_reports"]) == 3
        assert "version" not in report_json["validation_reports"][0]["report"]
        assert report_json["validation_reports"][0]["report"]["issues"][0]["code"] == (
            "evidence.source_digest_mismatch"
        )
        markdown = service.render_report(started["run"].id, "markdown")
        assert "## Validation failures" in markdown
        assert "evidence.source_digest_mismatch" in markdown


def test_session_callback_is_enough_to_start_the_one_automatic_correction(tmp_path):
    repo = make_repository(tmp_path)
    runtime = CallbackOnlySessionRuntime()
    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        task_view = next(item for item in started["tasks"] if item["task"].role == AgentRole.SUBSTANTIVE_REVIEW)

        assert len(task_view["attempts"]) == 2
        assert task_view["attempts"][0].thread_id == "thread-substantive_review-1"
        assert runtime.resume_calls == [AgentRole.SUBSTANTIVE_REVIEW]


def test_different_route_starts_self_contained_session_with_report(tmp_path):
    repo = make_repository(tmp_path)
    local_path = repo / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8")
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
    runtime = RepeatedInvalidRuntime()
    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        source_task = next(
            item["task"] for item in started["tasks"] if item["task"].role == AgentRole.SUBSTANTIVE_REVIEW
        )
        retried = asyncio.run(service.retry_task(started["run"].id, source_task.id, "alternate"))

        tasks = [item["task"] for item in retried["tasks"] if item["task"].role == AgentRole.SUBSTANTIVE_REVIEW]
        assert {task.route for task in tasks} == {"primary", "alternate"}
        assert len(runtime.run_prompts) == 2
        alternate_prompt = runtime.run_prompts[-1]
        assert "Review the frozen manuscript in the workspace" in alternate_prompt
        assert "Validation report digest:" in alternate_prompt


def test_interrupted_correction_reuses_exact_prompt_artifact(tmp_path):
    repo = make_repository(tmp_path)
    runtime = RepeatedInvalidRuntime(interrupt_first_correction=True)
    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        task_view = next(item for item in started["tasks"] if item["task"].role == AgentRole.SUBSTANTIVE_REVIEW)
        interrupted = task_view["attempts"][-1]
        assert interrupted.status == AttemptStatus.INTERRUPTED

        retried = asyncio.run(service.retry_task(started["run"].id, task_view["task"].id))
        retried_view = next(item for item in retried["tasks"] if item["task"].id == task_view["task"].id)
        assert retried_view["attempts"][-1].prompt_digest == interrupted.prompt_digest
        assert runtime.resume_prompts[1] == runtime.resume_prompts[0]


def test_corrupt_report_blocks_retry_before_new_attempt(tmp_path):
    repo = make_repository(tmp_path)
    runtime = RepeatedInvalidRuntime()
    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        task_view = next(item for item in started["tasks"] if item["task"].role == AgentRole.SUBSTANTIVE_REVIEW)
        attempts_before = list(task_view["attempts"])
        digest = attempts_before[-1].validation_report_artifact_digest
        service.artifacts.path_for(digest).write_bytes(b"corrupt")

        with pytest.raises(InfrastructureError, match="missing, corrupt, or unsupported"):
            asyncio.run(service.retry_task(started["run"].id, task_view["task"].id))

        assert service.database.list_attempts(task_view["task"].id) == attempts_before


def test_report_media_type_and_provenance_bindings_are_enforced(tmp_path):
    repo = make_repository(tmp_path)
    runtime = RepeatedInvalidRuntime()
    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        started = asyncio.run(service.start_run("HEAD", "quick", None))
        task_view = next(item for item in started["tasks"] if item["task"].role == AgentRole.SUBSTANTIVE_REVIEW)
        attempt = task_view["attempts"][-1]
        original = service.armarius._load_validation_report(
            attempt,
            "review",
            attempt.schema_digest,
            attempt.bundle_digest,
        )
        issue = original.issues[0]

        wrong_bundle_artifact, _ = service.armarius._record_validation_report(
            "review",
            attempt.schema_digest,
            "f" * 64,
            attempt.output_artifact_digest,
            [issue],
        )
        wrong_bundle_attempt = replace(
            attempt,
            validation_report_artifact_digest=wrong_bundle_artifact.digest,
        )
        with pytest.raises(InfrastructureError, match="mismatched bundle digest"):
            service.armarius._load_validation_report(
                wrong_bundle_attempt,
                "review",
                attempt.schema_digest,
                attempt.bundle_digest,
            )

        generic_artifact = service.armarius._record_text(
            json.dumps({"diagnostic": "not a validation report"}),
            "application/json",
        )
        wrong_media_attempt = replace(
            attempt,
            validation_report_artifact_digest=generic_artifact.digest,
        )
        with pytest.raises(InfrastructureError, match="invalid media type"):
            service.armarius._load_validation_report(
                wrong_media_attempt,
                "review",
                attempt.schema_digest,
                attempt.bundle_digest,
            )


def test_report_digest_is_content_deterministic_and_projection_is_bounded(tmp_path):
    _, service = _service(tmp_path)
    with service:
        issue = service.armarius._issue(
            "schema.invalid",
            "/value",
            "Invalid value.",
            expected="x" * 3_000,
            actual="y" * 3_000,
            diff="z" * 5_000,
        )
        first, report = service.armarius._record_validation_report(
            "review",
            "a" * 64,
            "b" * 64,
            None,
            [issue] * 40,
        )
        second, repeated = service.armarius._record_validation_report(
            "review",
            "a" * 64,
            "b" * 64,
            None,
            [issue] * 40,
        )
        projection = service.armarius._validation_projection(report)

    assert first.digest == second.digest
    assert report == repeated
    assert len(json.dumps(projection, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()) <= 64 * 1024
    assert projection["omitted_issue_count"] > 0
    assert issue.expected["original_codepoints"] == 3_000
    assert len(issue.diff) <= 4_000
    assert "truncated from 5000 code points" in issue.diff
    assert ValidationReport.model_validate_json(service.artifacts.get_bytes(first.digest)) == report

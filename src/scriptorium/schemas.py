from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Severity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MODERATE = "moderate"
    MINOR = "minor"
    SUGGESTION = "suggestion"


class Evidence(StrictModel):
    source_path: str = Field(min_length=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    source_digest: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    page: int | None = Field(default=None, ge=1)
    quoted_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_anchor(self) -> "Evidence":
        has_source = self.start_line is not None or self.end_line is not None or self.source_digest is not None
        if has_source and None in {self.start_line, self.end_line, self.source_digest}:
            raise ValueError("source evidence requires line range and source_digest")
        if self.start_line is not None and self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        if not has_source and self.page is None:
            raise ValueError("evidence requires a source or PDF page anchor")
        return self


class FindingCandidate(StrictModel):
    category: str = Field(min_length=1)
    severity: Severity
    title: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    explanation: str = Field(min_length=1)
    suggested_action: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class ReviewOutput(StrictModel):
    summary: str = Field(min_length=1)
    findings: list[FindingCandidate]


class VisualTranscriptionPage(StrictModel):
    page: int = Field(ge=1)
    page_digest: str = Field(pattern="^[0-9a-f]{64}$")
    text: str


class VisualTranscriptionOutput(StrictModel):
    pdf_digest: str = Field(pattern="^[0-9a-f]{64}$")
    pages: list[VisualTranscriptionPage] = Field(min_length=1)


class ExactEdit(StrictModel):
    finding_ids: list[str] = Field(min_length=1)
    path: str = Field(min_length=1)
    source_digest: str = Field(pattern="^[0-9a-f]{64}$")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    before: str = Field(min_length=1)
    after: str
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self) -> "ExactEdit":
        if self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        if self.before == self.after:
            raise ValueError("before and after must differ")
        return self


class RevisionOutput(StrictModel):
    summary: str = Field(min_length=1)
    edits: list[ExactEdit]


class VerificationIssue(StrictModel):
    title: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)


class VerificationOutput(StrictModel):
    verdict: str = Field(pattern="^(pass|fail)$")
    summary: str = Field(min_length=1)
    resolved_finding_ids: list[str]
    issues: list[VerificationIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_verdict(self) -> "VerificationOutput":
        if self.verdict == "pass" and self.issues:
            raise ValueError("passing verification cannot contain issues")
        return self


class ValidationIssue(StrictModel):
    code: str
    path: str
    message: str
    expected: Any = None
    actual: Any = None
    diff: str | None = None


class ValidationReport(StrictModel):
    schema_kind: Literal["review", "visual_transcription", "revision", "verification"]
    schema_digest: str = Field(pattern="^[0-9a-f]{64}$")
    bundle_digest: str = Field(pattern="^[0-9a-f]{64}$")
    output_artifact_digest: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    issues: list[ValidationIssue] = Field(min_length=1)


SCHEMA_MODELS: dict[str, type[StrictModel]] = {
    "review": ReviewOutput,
    "visual_transcription": VisualTranscriptionOutput,
    "revision": RevisionOutput,
    "verification": VerificationOutput,
}


def output_schema(kind: str) -> dict[str, Any]:
    return SCHEMA_MODELS[kind].model_json_schema()


def parse_output(kind: str, value: str) -> StrictModel:
    return SCHEMA_MODELS[kind].model_validate_json(value)

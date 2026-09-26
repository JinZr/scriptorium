from __future__ import annotations

from decimal import Decimal
from enum import Enum
import sys
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import digest_json


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _EvidenceRule(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_fields: tuple[str, ...]
    forbidden_fields: tuple[str, ...]
    source_path_rule: str
    matching_rule: str
    source_path: str | None = None


class EvidenceAnchorContract(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_line: _EvidenceRule
    pdf_page: _EvidenceRule
    revision_edit: _EvidenceRule

    @model_validator(mode="after")
    def validate_shapes(self) -> "EvidenceAnchorContract":
        if (
            self.source_line.required_fields
            != (
                "source_path",
                "start_line",
                "end_line",
                "source_digest",
                "quoted_text",
            )
            or self.source_line.forbidden_fields != ("page",)
            or self.source_line.source_path is not None
        ):
            raise ValueError("source-line evidence fields do not match the local parser")
        if (
            self.pdf_page.required_fields != ("source_path", "page")
            or self.pdf_page.forbidden_fields != ("start_line", "end_line", "source_digest", "quoted_text")
            or self.pdf_page.source_path != "manuscript.pdf"
        ):
            raise ValueError("PDF-page evidence fields do not match the local parser")
        if (
            self.revision_edit.required_fields
            != (
                "path",
                "source_digest",
                "start_line",
                "end_line",
                "before",
            )
            or self.revision_edit.forbidden_fields
            or self.revision_edit.source_path is not None
        ):
            raise ValueError("revision edit fields do not match the local parser")
        return self


DEFAULT_EVIDENCE_ANCHOR_CONTRACT = EvidenceAnchorContract(
    source_line=_EvidenceRule(
        required_fields=(
            "source_path",
            "start_line",
            "end_line",
            "source_digest",
            "quoted_text",
        ),
        forbidden_fields=("page",),
        source_path_rule="bare relative path listed in source-map.json",
        matching_rule="quoted_text is a verbatim substring of the inclusive UTF-8 source line range",
    ),
    pdf_page=_EvidenceRule(
        required_fields=("source_path", "page"),
        forbidden_fields=("start_line", "end_line", "source_digest", "quoted_text"),
        source_path_rule="compiled PDF path",
        matching_rule="page identifies rendered content bound by the frozen bundle and page digest",
        source_path="manuscript.pdf",
    ),
    revision_edit=_EvidenceRule(
        required_fields=(
            "path",
            "source_digest",
            "start_line",
            "end_line",
            "before",
        ),
        forbidden_fields=(),
        source_path_rule="bare relative text-anchorable path listed in source-map.json",
        matching_rule=(
            "before exactly matches the inclusive UTF-8 source line range, either with its existing final line "
            "terminator or without that terminator"
        ),
    ),
)


def evidence_anchor_contract_content(
    contract: EvidenceAnchorContract = DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
) -> dict[str, Any]:
    return contract.model_dump(mode="json", exclude_none=True)


def evidence_anchor_contract_digest(
    contract: EvidenceAnchorContract = DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
) -> str:
    return digest_json(evidence_anchor_contract_content(contract))


class SourceAnchorRecord(StrictModel):
    source_path: str = Field(min_length=1)
    read_path: str = Field(min_length=1)
    source_digest: str = Field(pattern="^[0-9a-f]{64}$")
    line_count: int | None = Field(default=None, ge=0)
    text_anchorable: bool

    @model_validator(mode="after")
    def validate_source(self) -> "SourceAnchorRecord":
        if self.read_path != f"sources/{self.source_path}":
            raise ValueError("read_path must map to the source_path under sources/")
        if self.text_anchorable != (self.line_count is not None):
            raise ValueError("only text-anchorable sources have a line_count")
        return self


class CompiledPdfPageRecord(StrictModel):
    page: int = Field(ge=1)
    read_path: str = Field(min_length=1)
    page_digest: str = Field(pattern="^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_read_path(self) -> "CompiledPdfPageRecord":
        if self.read_path != f"pages/page-{self.page:04d}.png":
            raise ValueError("read_path must use the canonical zero-padded page path")
        return self


class CompiledPdfAnchor(StrictModel):
    source_path: Literal["manuscript.pdf"]
    read_path: Literal["manuscript.pdf"]
    page_count: int = Field(ge=1)
    pages: list[CompiledPdfPageRecord]

    @model_validator(mode="after")
    def validate_pages(self) -> "CompiledPdfAnchor":
        if [item.page for item in self.pages] != list(range(1, self.page_count + 1)):
            raise ValueError("pages must contain every compiled PDF page in order")
        return self


class EvidenceAnchorMap(StrictModel):
    contract_digest: str = Field(pattern="^[0-9a-f]{64}$")
    sources: list[SourceAnchorRecord]
    compiled_pdf: CompiledPdfAnchor

    @model_validator(mode="after")
    def validate_sources(self) -> "EvidenceAnchorMap":
        source_paths = [item.source_path for item in self.sources]
        if len(source_paths) != len(set(source_paths)):
            raise ValueError("source_path values must be unique")
        return self


class Severity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MODERATE = "moderate"
    MINOR = "minor"
    SUGGESTION = "suggestion"


class Evidence(StrictModel):
    source_path: str = Field(
        min_length=1,
        description=(
            f"Source-line evidence uses a {DEFAULT_EVIDENCE_ANCHOR_CONTRACT.source_line.source_path_rule}; "
            f"PDF-page evidence uses {DEFAULT_EVIDENCE_ANCHOR_CONTRACT.pdf_page.source_path}."
        ),
    )
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    source_digest: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    page: int | None = Field(default=None, ge=1)
    quoted_text: str | None = Field(default=None, min_length=1)

    @model_validator(mode="before")
    @classmethod
    def validate_forbidden_fields(cls, value: Any) -> Any:
        if isinstance(value, dict) and "page" in value:
            # Key presence matters so explicit null cannot bypass the frozen mutually exclusive shape.
            forbidden = {"start_line", "end_line", "source_digest", "quoted_text"}
            if forbidden.intersection(value):
                raise ValueError("PDF page evidence cannot include source line or quoted-text fields")
        return value

    @model_validator(mode="after")
    def validate_anchor(self) -> "Evidence":
        source_fields = (self.start_line, self.end_line, self.source_digest)
        if self.page is not None and any(value is not None for value in source_fields):
            raise ValueError("PDF page evidence cannot include source line fields")
        if self.page is None and (any(value is None for value in source_fields) or self.quoted_text is None):
            raise ValueError("source evidence requires line range, source_digest, and quoted_text")
        if self.start_line is not None and self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
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


class ReviewScopeArea(StrictModel):
    source_path: str = Field(min_length=1)
    start_line: int | None = Field(default=None, ge=1, strict=True)
    end_line: int | None = Field(default=None, ge=1, strict=True)
    page: int | None = Field(default=None, ge=1, strict=True)

    @field_validator("start_line", "end_line", "page", mode="before")
    @classmethod
    def accept_integral_number(cls, value: Any) -> Any:
        if isinstance(value, Decimal):
            if value.is_finite() and 1 <= value <= sys.maxsize and value == value.to_integral_value():
                return int(value)
            return value
        if isinstance(value, float) and 1 <= value <= sys.maxsize and value.is_integer():
            return int(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def validate_field_presence(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if value.get("source_path") == "manuscript.pdf":
            if {"start_line", "end_line"}.intersection(value):
                raise ValueError("compiled PDF scope cannot include line fields")
        else:
            if "page" in value:
                raise ValueError("source scope cannot include a page field")
            lines = {"start_line", "end_line"}.intersection(value)
            if lines and (len(lines) != 2 or any(value[line] is None for line in lines)):
                raise ValueError("source scope requires both non-null line endpoints or neither")
        return value

    @model_validator(mode="after")
    def validate_location(self) -> "ReviewScopeArea":
        if self.source_path == "manuscript.pdf":
            if self.page is None or self.start_line is not None or self.end_line is not None:
                raise ValueError("compiled PDF scope requires only a page")
        elif self.page is not None or (self.start_line is None) != (self.end_line is None):
            raise ValueError("source scope requires both line endpoints or neither")
        elif self.start_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        return self


class ReviewScope(StrictModel):
    completion: Literal["complete", "partial", "unknown"]
    checked: list[ReviewScopeArea]
    outstanding: list[ReviewScopeArea]
    limitations: list[str]

    @model_validator(mode="after")
    def validate_completion(self) -> "ReviewScope":
        if self.completion == "complete" and self.outstanding:
            raise ValueError("complete scope cannot include outstanding areas")
        return self


class ScopedReviewOutput(ReviewOutput):
    model_config = ConfigDict(extra="forbid", title="ReviewOutput")

    scope: ReviewScope


class ClaimCheck(StrictModel):
    claim: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    critical_question: str = Field(min_length=1)
    countercheck: str = Field(min_length=1)
    assessment: Literal["supported", "unresolved", "finding"]
    finding_indices: list[Annotated[int, Field(ge=0, strict=True)]]


class ScientificReviewOutput(ScopedReviewOutput):
    claim_checks: list[ClaimCheck]

    @model_validator(mode="after")
    def validate_claim_checks(self) -> "ScientificReviewOutput":
        if self.scope.completion == "complete" and not self.claim_checks:
            raise ValueError("a complete substantive review requires claim checks")
        linked: set[int] = set()
        for check in self.claim_checks:
            if (check.assessment == "finding") != bool(check.finding_indices):
                raise ValueError("only a retained claim check may link findings")
            if any(index >= len(self.findings) for index in check.finding_indices):
                raise ValueError("claim check refers to an unknown finding index")
            linked.update(check.finding_indices)
        if linked != set(range(len(self.findings))):
            raise ValueError("every substantive finding must be linked to a claim check")
        return self


# Historical runs still deserialize these persisted outputs, but new runs never schedule this role.
class VisualTranscriptionPage(StrictModel):
    page: int = Field(ge=1)
    page_digest: str = Field(pattern="^[0-9a-f]{64}$")
    text: str


class VisualTranscriptionOutput(StrictModel):
    pdf_digest: str = Field(pattern="^[0-9a-f]{64}$")
    pages: list[VisualTranscriptionPage] = Field(min_length=1)


class ExactEdit(StrictModel):
    finding_ids: list[str] = Field(min_length=1)
    path: str = Field(
        min_length=1,
        description=DEFAULT_EVIDENCE_ANCHOR_CONTRACT.revision_edit.source_path_rule,
    )
    source_digest: str = Field(
        pattern="^[0-9a-f]{64}$",
        description="exact source_digest from source-map.json",
    )
    start_line: int = Field(ge=1, description="first inclusive source line")
    end_line: int = Field(ge=1, description="last inclusive source line")
    before: str = Field(
        min_length=1,
        description=DEFAULT_EVIDENCE_ANCHOR_CONTRACT.revision_edit.matching_rule,
    )
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
    schema_kind: Literal["review", "scientific_review", "visual_transcription", "revision", "verification"]
    schema_digest: str = Field(pattern="^[0-9a-f]{64}$")
    bundle_digest: str = Field(pattern="^[0-9a-f]{64}$")
    output_artifact_digest: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    issues: list[ValidationIssue] = Field(min_length=1)


SCHEMA_MODELS: dict[str, type[StrictModel]] = {
    "review": ScopedReviewOutput,
    "scientific_review": ScientificReviewOutput,
    "visual_transcription": VisualTranscriptionOutput,
    "revision": RevisionOutput,
    "verification": VerificationOutput,
}


def output_schema(
    kind: str,
    contract: EvidenceAnchorContract = DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    *,
    legacy_review: bool = False,
) -> dict[str, Any]:
    model = ReviewOutput if kind == "review" and legacy_review else SCHEMA_MODELS[kind]
    schema = model.model_json_schema()
    if kind in {"review", "scientific_review", "verification"}:
        evidence = schema["$defs"]["Evidence"]
        evidence["properties"]["source_path"]["description"] = (
            f"Source-line evidence uses a {contract.source_line.source_path_rule}; "
            f"PDF-page evidence uses {contract.pdf_page.source_path}."
        )
        evidence["properties"]["quoted_text"]["description"] = contract.source_line.matching_rule
        evidence["oneOf"] = _evidence_anchor_schema(contract)
        if kind in {"review", "scientific_review"} and not legacy_review:
            schema["$defs"]["ReviewScopeArea"]["oneOf"] = [
                {
                    "title": "Compiled PDF page",
                    "required": ["page"],
                    "properties": {
                        "source_path": {"const": "manuscript.pdf"},
                        "page": {"type": "integer", "minimum": 1},
                    },
                    "not": {"anyOf": [{"required": ["start_line"]}, {"required": ["end_line"]}]},
                },
                {
                    "title": "Source path or line range",
                    "properties": {
                        "source_path": {"not": {"const": "manuscript.pdf"}},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "not": {"required": ["page"]},
                    "oneOf": [
                        {"required": ["start_line", "end_line"]},
                        {"not": {"anyOf": [{"required": ["start_line"]}, {"required": ["end_line"]}]}},
                    ],
                },
            ]
            schema["$defs"]["ReviewScope"]["allOf"] = [
                {
                    "if": {"properties": {"completion": {"const": "complete"}}, "required": ["completion"]},
                    "then": {"properties": {"outstanding": {"maxItems": 0}}},
                }
            ]
    elif kind == "revision":
        properties = schema["$defs"]["ExactEdit"]["properties"]
        properties["path"]["description"] = contract.revision_edit.source_path_rule
        properties["before"]["description"] = contract.revision_edit.matching_rule
    return schema


def _evidence_anchor_schema(contract: EvidenceAnchorContract) -> list[dict[str, Any]]:
    source_line = contract.source_line
    pdf_page = contract.pdf_page
    return [
        {
            "title": "Source line evidence",
            "required": [field for field in source_line.required_fields if field != "source_path"],
            "not": {"anyOf": [{"required": [field]} for field in source_line.forbidden_fields]},
            "properties": {
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "source_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "quoted_text": {"type": "string", "minLength": 1},
            },
        },
        {
            "title": "Compiled PDF page evidence",
            "required": [field for field in pdf_page.required_fields if field != "source_path"],
            "not": {"anyOf": [{"required": [field]} for field in pdf_page.forbidden_fields]},
            "properties": {
                "source_path": {"const": pdf_page.source_path},
                "page": {"type": "integer", "minimum": 1},
            },
        },
    ]


def parse_output(kind: str, value: str) -> StrictModel:
    return SCHEMA_MODELS[kind].model_validate_json(value)

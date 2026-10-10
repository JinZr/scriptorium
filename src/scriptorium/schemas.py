from __future__ import annotations

from decimal import Decimal
from enum import Enum
import re
import sys
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

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


UNIVERSAL_LINE_TERMINATORS = ("\r\n", "\r", "\n")
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


class EvidenceAnchorContract(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_line: _EvidenceRule
    pdf_page: _EvidenceRule
    revision_edit: _EvidenceRule
    # Contracts frozen before task retrieval and validation shared a line definition omit this field; their runs
    # keep str.splitlines(), which also breaks at form feeds and Unicode line separators.
    line_terminators: tuple[str, ...] | None = None

    def split_lines(self, text: str, *, keepends: bool = False) -> list[str]:
        """Split source text into the numbered lines this frozen contract anchors."""
        if self.line_terminators is None:
            return text.splitlines(keepends)
        lines = []
        start = 0
        for match in _LINE_BREAK.finditer(text):
            lines.append(text[start : match.end() if keepends else match.start()])
            start = match.end()
        if start < len(text):
            lines.append(text[start:])
        return lines

    @model_validator(mode="after")
    def validate_shapes(self) -> "EvidenceAnchorContract":
        if self.line_terminators is not None and self.line_terminators != UNIVERSAL_LINE_TERMINATORS:
            raise ValueError("source line terminators do not match the local parser")
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
    line_terminators=UNIVERSAL_LINE_TERMINATORS,
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


class CompiledPdfDocumentRecord(StrictModel):
    entrypoint: str = Field(min_length=1)
    start_page: int = Field(ge=1)
    page_count: int = Field(ge=1)


class CompiledPdfAnchor(StrictModel):
    source_path: Literal["manuscript.pdf"]
    read_path: Literal["manuscript.pdf"]
    page_count: int = Field(ge=1)
    pages: list[CompiledPdfPageRecord]
    documents: list[CompiledPdfDocumentRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_pages(self) -> "CompiledPdfAnchor":
        if [item.page for item in self.pages] != list(range(1, self.page_count + 1)):
            raise ValueError("pages must contain every compiled PDF page in order")
        next_page = 1
        for document in self.documents:
            if document.start_page != next_page:
                raise ValueError("document page ranges must be contiguous and ordered")
            next_page += document.page_count
        if self.documents and next_page != self.page_count + 1:
            raise ValueError("document page ranges must cover the compiled PDF")
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
        entrypoints = [item.entrypoint for item in self.compiled_pdf.documents]
        text_paths = {item.source_path for item in self.sources if item.text_anchorable}
        if len(entrypoints) != len(set(entrypoints)) or not set(entrypoints) <= text_paths:
            raise ValueError("document entrypoints must be distinct frozen text sources")
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


SUBMISSION_COMPLIANCE_CATEGORY = "submission_compliance"
_COMPLIANCE_SEVERITIES = (Severity.MINOR, Severity.SUGGESTION)
SEVERITY_DEFINITIONS = {
    Severity.BLOCKER: "a headline claim is false as stated, or the manuscript cannot be evaluated (missing core "
    "result, an argument that cannot be followed).",
    Severity.MAJOR: "a headline claim needs substantive qualification, or a key result cannot be reproduced or "
    "traced from the manuscript; a reviewer at the target venue would recommend rejection or return on this basis "
    "alone.",
    Severity.MODERATE: "a supporting claim is affected, or a reader would misread a specific result or comparison.",
    Severity.MINOR: "a presentation, consistency, or compliance problem that does not change how any claim should "
    "be read.",
    Severity.SUGGESTION: "an optional improvement.",
}
SEVERITY_RULES = (
    "Assign a level only when its definition is fully met; a problem that does not change how a reader should "
    "interpret any claim is minor or suggestion no matter how many places it appears.",
    "Do not assign moderate or above unless consequence states a concrete misreading or failure.",
    "A finding at moderate or above names the claim whose reading it changes, in affected_claim or, in a substantive "
    "review, through its claim check; a blocker needs a headline claim.",
)
_CONSEQUENCE = "what a reader would wrongly believe, or be unable to do, if this is not fixed"
_COMPLIANCE_RULE = (
    f"Checklist answers, page or figure limits, anonymity, and template rules use category "
    f"{SUBMISSION_COMPLIANCE_CATEGORY}, at minor or suggestion."
)
SEVERITY_RUBRIC = "\n".join(
    (
        "Severity rubric, by consequence:",
        *(f"- {level.value}: {definition}" for level, definition in SEVERITY_DEFINITIONS.items()),
        SEVERITY_RULES[0],
        f"A finding's consequence states {_CONSEQUENCE}. {SEVERITY_RULES[1]}",
        SEVERITY_RULES[2],
        _COMPLIANCE_RULE,
    )
)


class RatedFinding(FindingCandidate):
    category: str = Field(min_length=1, description=f"A short finding category. {_COMPLIANCE_RULE}")
    severity: Severity = Field(
        description=" ".join(
            (
                *(f"{level.value}: {definition}" for level, definition in SEVERITY_DEFINITIONS.items()),
                *SEVERITY_RULES,
            )
        )
    )
    consequence: str = Field(min_length=1, description=f"One or two sentences: {_CONSEQUENCE}.")

    @field_validator("consequence")
    @classmethod
    def require_consequence_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("consequence must state what a reader would wrongly believe or be unable to do")
        return value

    @model_validator(mode="after")
    def validate_compliance_severity(self) -> "RatedFinding":
        if self.category == SUBMISSION_COMPLIANCE_CATEGORY and self.severity not in _COMPLIANCE_SEVERITIES:
            raise ValueError(
                f"a {SUBMISSION_COMPLIANCE_CATEGORY} finding must be minor or suggestion: a checklist, limit, "
                "anonymity, or template problem does not change how any claim should be read"
            )
        return self


_CLAIM_SEVERITIES = (Severity.BLOCKER, Severity.MAJOR, Severity.MODERATE)


class ClaimedFinding(RatedFinding):
    affected_claim: str | None = Field(
        default=None,
        min_length=1,
        description="The manuscript claim or result whose reading changes because of this finding, in one sentence. "
        "Required at moderate or above.",
    )

    @model_validator(mode="after")
    def validate_affected_claim(self) -> "ClaimedFinding":
        if self.severity in _CLAIM_SEVERITIES and (self.affected_claim is None or not self.affected_claim.strip()):
            raise ValueError(
                f"a {self.severity.value} finding must name its affected_claim, the claim or result whose reading "
                "it changes, or be rated minor or suggestion"
            )
        return self


class ReviewOutput(StrictModel):
    summary: str = Field(min_length=1)
    findings: list[FindingCandidate]


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float, Decimal)):
        return "a number"
    return {str: "a string", list: "an array"}.get(type(value), type(value).__name__)


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
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise PydanticCustomError(
                "scope_area_type",
                "each scope.checked and scope.outstanding entry must be an object, not {kind}: give source_path "
                "with optional start_line and end_line together, or source_path manuscript.pdf with page",
                {"kind": _json_kind(value)},
            )
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


class RatedReviewOutput(ScopedReviewOutput):
    findings: list[RatedFinding]


class ClaimedReviewOutput(RatedReviewOutput):
    findings: list[ClaimedFinding]


def _integral_indices(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [
        (
            int(index)
            if isinstance(index, Decimal)
            and index.is_finite()
            and 0 <= index <= sys.maxsize
            and index == index.to_integral_value()
            else index
        )
        for index in value
    ]


class ClaimCheck(StrictModel):
    claim: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    critical_question: str = Field(min_length=1)
    countercheck: str = Field(min_length=1)
    assessment: Literal["supported", "unresolved", "finding"]
    finding_indices: list[Annotated[int, Field(ge=0, strict=True)]]

    @field_validator("finding_indices", mode="before")
    @classmethod
    def accept_integral_indices(cls, value: Any) -> Any:
        return _integral_indices(value)


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


class Recomputation(StrictModel):
    inputs: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1, description="Each reported value used, with where the manuscript states it."
    )
    calculation: str = Field(min_length=1, description="The arithmetic or derivation performed on those inputs.")
    result: str = Field(min_length=1, description="The value obtained, with its unit.")
    reported: str = Field(min_length=1, description="The manuscript value it is compared with, with its unit.")
    outcome: Literal["matches", "differs"] = Field(
        description='"matches" only when the result agrees with the reported value within the stated rounding.'
    )


class JudgedClaimCheck(ClaimCheck):
    claim_anchor: Evidence = Field(description="Where the authors state the claim, as an exact frozen anchor.")
    stated_scope: str = Field(
        min_length=1,
        description="The population, conditions, range, threshold, and qualifications under which the authors "
        "state the claim.",
    )
    check_type: Literal[
        "reporting_consistency",
        "recomputation",
        "design_and_analysis",
        "alternative_explanation",
        "scope_and_generality",
    ] = Field(description="What the countercheck examined.")
    question_answer: Literal["yes", "partly", "no", "not_checkable"] = Field(
        description="Whether the countercheck shows the claim holds at its stated scope: yes, partly, no, or "
        "not_checkable when the frozen bundle cannot decide it."
    )
    exceptions: list[Annotated[str, Field(min_length=1)]] = Field(
        description="Cases, conditions, or values within the stated scope where the claim fails or is not shown."
    )
    recomputation: Recomputation | None = Field(
        default=None, description='Required when check_type is "recomputation"; omit it otherwise.'
    )

    @model_validator(mode="after")
    def validate_judgment(self) -> "JudgedClaimCheck":
        _validate_judgment(self)
        return self


def _validate_judgment(check: JudgedClaimCheck | LinkedClaimCheck) -> None:
    if (check.check_type == "recomputation") != (check.recomputation is not None):
        raise ValueError('a recomputation is recorded exactly when check_type is "recomputation"')
    if (check.assessment == "supported") != (check.question_answer == "yes"):
        raise ValueError('assessment "supported" goes with question_answer "yes", and only with it')
    if check.question_answer == "yes" and check.exceptions:
        raise ValueError('question_answer "yes" lists no exceptions; use "partly"')
    if check.question_answer == "partly" and not check.exceptions:
        raise ValueError('question_answer "partly" lists at least one exception')
    if check.recomputation is not None and (check.recomputation.outcome, check.question_answer) in {
        ("matches", "no"),
        ("differs", "yes"),
    }:
        raise ValueError('a recomputation that differs cannot answer "yes", and one that matches cannot answer "no"')
    if check.question_answer == "not_checkable" and check.assessment != "unresolved":
        raise ValueError('question_answer "not_checkable" requires assessment "unresolved"')


class JudgedScientificReviewOutput(ScientificReviewOutput):
    claim_checks: list[JudgedClaimCheck]


class InventoriedClaim(StrictModel):
    claim: str = Field(min_length=1, description="The central claim as the authors state it.")
    claim_anchor: Evidence = Field(description="Where the authors state the claim, as an exact frozen anchor.")
    prominence: Literal["headline", "supporting"] = Field(
        description='"headline" for a claim in the abstract, stated contributions, or conclusions; "supporting" '
        "for a claim the headline claims depend on."
    )
    check_indices: list[Annotated[int, Field(ge=0, strict=True)]] = Field(
        description="Zero-based positions in this output's claim_checks that assess this claim."
    )
    not_checked_reason: str | None = Field(
        default=None,
        min_length=1,
        description="Why this submission has no claim check for the claim; omit it when check_indices is not empty.",
    )

    @field_validator("check_indices", mode="before")
    @classmethod
    def accept_integral_indices(cls, value: Any) -> Any:
        return _integral_indices(value)

    @model_validator(mode="after")
    def validate_status(self) -> "InventoriedClaim":
        if bool(self.check_indices) == (self.not_checked_reason is not None):
            raise ValueError("an inventoried claim lists check_indices or a not_checked_reason, not both or neither")
        return self


class InventoriedScientificReviewOutput(JudgedScientificReviewOutput):
    claim_inventory: list[InventoriedClaim] = Field(
        description="The central claims identified in the manuscript, each linked to its claim checks or left "
        "unchecked with a reason."
    )

    @model_validator(mode="after")
    def validate_claim_inventory(self) -> "InventoriedScientificReviewOutput":
        listed: list[int] = []
        for entry in self.claim_inventory:
            if any(index >= len(self.claim_checks) for index in entry.check_indices):
                raise ValueError("an inventoried claim refers to an unknown claim check index")
            if any(
                (self.claim_checks[index].claim, self.claim_checks[index].claim_anchor)
                != (entry.claim, entry.claim_anchor)
                for index in entry.check_indices
            ):
                raise ValueError("a linked claim check must restate its inventoried claim and claim_anchor exactly")
            listed.extend(entry.check_indices)
        if sorted(listed) != list(range(len(self.claim_checks))):
            raise ValueError("each claim check must assess exactly one inventoried claim")
        if self.scope.completion == "complete" and any(
            entry.prominence == "headline" and not entry.check_indices for entry in self.claim_inventory
        ):
            raise ValueError("a complete substantive review must check every inventoried headline claim")
        return self


class RatedScientificReviewOutput(InventoriedScientificReviewOutput):
    findings: list[RatedFinding]


class LinkedClaimCheck(StrictModel):
    claim_index: int = Field(
        ge=0, strict=True, description="Zero-based position in this output's claim_inventory of the claim assessed."
    )
    evidence: list[Evidence] = Field(min_length=1)
    critical_question: str = Field(min_length=1)
    countercheck: str = Field(min_length=1)
    stated_scope: str = Field(
        min_length=1,
        description="The population, conditions, range, threshold, and qualifications under which the authors "
        "state the claim.",
    )
    check_type: Literal[
        "reporting_consistency",
        "recomputation",
        "design_and_analysis",
        "alternative_explanation",
        "scope_and_generality",
    ] = Field(description="What the countercheck examined.")
    question_answer: Literal["yes", "partly", "no", "not_checkable"] = Field(
        description="Whether the countercheck shows the claim holds at its stated scope: yes, partly, no, or "
        "not_checkable when the frozen bundle cannot decide it."
    )
    exceptions: list[Annotated[str, Field(min_length=1)]] = Field(
        description="Cases, conditions, or values within the stated scope where the claim fails or is not shown."
    )
    recomputation: Recomputation | None = Field(
        default=None, description='Required when check_type is "recomputation"; omit it otherwise.'
    )
    assessment: Literal["supported", "unresolved", "finding"]
    finding_indices: list[Annotated[int, Field(ge=0, strict=True)]]

    @field_validator("claim_index", mode="before")
    @classmethod
    def accept_integral_index(cls, value: Any) -> Any:
        return _integral_indices([value])[0]

    @field_validator("finding_indices", mode="before")
    @classmethod
    def accept_integral_indices(cls, value: Any) -> Any:
        return _integral_indices(value)

    @model_validator(mode="after")
    def validate_judgment(self) -> "LinkedClaimCheck":
        _validate_judgment(self)
        return self


class LinkedClaim(StrictModel):
    claim: str = Field(min_length=1, description="The central claim as the authors state it.")
    claim_anchor: Evidence = Field(description="Where the authors state the claim, as an exact frozen anchor.")
    prominence: Literal["headline", "supporting"] = Field(
        description='"headline" for a claim in the abstract, stated contributions, or conclusions; "supporting" '
        "for a claim the headline claims depend on."
    )
    not_checked_reason: str | None = Field(
        default=None,
        min_length=1,
        description="Why no claim check in this submission names this claim by claim_index; omit it when one does.",
    )


class Verdict(StrictModel):
    recommendation: Literal["accept", "minor_revision", "major_revision", "reject"] = Field(
        description="Decided before listing findings; reject or major_revision rests on at least one major or "
        "blocker finding of this review, and accept allows none."
    )
    decisive_questions: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1,
        max_length=3,
        description="One to three questions whose answers would change the recommendation.",
    )


_HIGH_SEVERITIES = (Severity.BLOCKER, Severity.MAJOR)


class LinkedScientificReviewOutput(RatedScientificReviewOutput):
    claim_checks: list[LinkedClaimCheck]
    claim_inventory: list[LinkedClaim] = Field(
        description="The central claims identified in the manuscript; claim checks refer to them by claim_index."
    )
    verdict: Verdict

    @model_validator(mode="after")
    def validate_claim_inventory(self) -> "LinkedScientificReviewOutput":
        if any(check.claim_index >= len(self.claim_inventory) for check in self.claim_checks):
            raise ValueError("a claim check refers to an unknown claim_index")
        referenced = {check.claim_index for check in self.claim_checks}
        for index, entry in enumerate(self.claim_inventory):
            if (index in referenced) == (entry.not_checked_reason is not None):
                raise ValueError(
                    "an inventoried claim has a not_checked_reason exactly when no claim check names it by claim_index"
                )
        if self.scope.completion == "complete" and any(
            entry.prominence == "headline" and index not in referenced
            for index, entry in enumerate(self.claim_inventory)
        ):
            raise ValueError("a complete substantive review must check every inventoried headline claim")
        headline_findings = {
            finding_index
            for check in self.claim_checks
            if self.claim_inventory[check.claim_index].prominence == "headline"
            for finding_index in check.finding_indices
        }
        if any(
            finding.severity == Severity.BLOCKER and index not in headline_findings
            for index, finding in enumerate(self.findings)
        ):
            raise ValueError("a blocker finding needs a headline claim: link it from a check of a headline claim")
        if self.verdict.recommendation == "accept" and any(
            finding.severity in _HIGH_SEVERITIES for finding in self.findings
        ):
            raise ValueError("an accept verdict cannot be submitted with a major or blocker finding")
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
    "review": ClaimedReviewOutput,
    "scientific_review": LinkedScientificReviewOutput,
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

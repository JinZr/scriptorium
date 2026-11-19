import copy

from pydantic import ValidationError
import pytest

from scriptorium.schemas import (
    DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    SCHEMA_MODELS,
    Evidence,
    EvidenceAnchorContract,
    EvidenceAnchorMap,
    LinkedClaimCheck,
    ReviewOutput,
    ScientificReviewOutput,
    evidence_anchor_contract_content,
    evidence_anchor_contract_digest,
    output_schema,
)


def test_evidence_requires_complete_anchor() -> None:
    with pytest.raises(ValidationError):
        Evidence(source_path="main.tex", start_line=1, quoted_text="claim")


def test_evidence_requires_exactly_one_anchor_shape() -> None:
    digest = "a" * 64
    source = Evidence(
        source_path="main.tex",
        start_line=1,
        end_line=2,
        source_digest=digest,
        quoted_text="claim",
    )
    page = Evidence(source_path="manuscript.pdf", page=2)

    assert source.page is None
    assert page.start_line is None
    with pytest.raises(ValidationError):
        Evidence(
            source_path="main.tex",
            start_line=1,
            end_line=2,
            source_digest=digest,
            page=2,
        )
    with pytest.raises(ValidationError):
        Evidence(
            source_path="main.tex",
            start_line=1,
            end_line=2,
            source_digest=digest,
            page=None,
            quoted_text="claim",
        )
    with pytest.raises(ValidationError):
        Evidence(
            source_path="manuscript.pdf",
            start_line=None,
            page=2,
            quoted_text="claim",
        )


def test_pdf_shape_leaves_path_for_semantic_validation() -> None:
    evidence = Evidence(source_path="pages/page-0001.png", page=1)

    assert evidence.source_path == "pages/page-0001.png"


def test_anchor_contract_is_content_addressed_and_not_a_runtime_schema() -> None:
    content = evidence_anchor_contract_content()

    assert EvidenceAnchorContract.model_validate(content) == DEFAULT_EVIDENCE_ANCHOR_CONTRACT
    assert evidence_anchor_contract_digest() == evidence_anchor_contract_digest(
        EvidenceAnchorContract.model_validate(copy.deepcopy(content))
    )
    assert "version" not in content
    assert EvidenceAnchorContract not in SCHEMA_MODELS.values()


def test_evidence_schema_exposes_mutually_exclusive_contract_shapes() -> None:
    for kind in ("review", "verification"):
        evidence_schema = output_schema(kind)["$defs"]["Evidence"]

        assert evidence_schema["additionalProperties"] is False
        assert evidence_schema["required"] == ["source_path"]
        source_line, pdf_page = evidence_schema["oneOf"]
        assert source_line["required"] == ["start_line", "end_line", "source_digest", "quoted_text"]
        assert source_line["not"] == {"anyOf": [{"required": ["page"]}]}
        assert pdf_page["required"] == ["page"]
        assert pdf_page["properties"]["source_path"] == {"const": "manuscript.pdf"}
        assert pdf_page["not"] == {
            "anyOf": [
                {"required": ["start_line"]},
                {"required": ["end_line"]},
                {"required": ["source_digest"]},
                {"required": ["quoted_text"]},
            ]
        }


@pytest.mark.parametrize("kind", ["review", "scientific_review"])
def test_review_schema_requires_a_limitations_list_of_strings(kind) -> None:
    schema = output_schema(kind)
    limitations = schema["properties"]["limitations"]

    assert "limitations" in schema["required"]
    assert "scope" not in schema["properties"]
    assert {"ReviewScope", "ReviewScopeArea"}.isdisjoint(schema["$defs"])
    assert limitations["type"] == "array"
    assert limitations["items"] == {"minLength": 1, "type": "string"}
    assert "could not check or could not read" in limitations["description"]
    assert "absent from the frozen sources" in limitations["description"]


def test_limitations_items_must_not_be_blank() -> None:
    assert ScientificReviewOutput.model_fields["limitations"].is_required()
    with pytest.raises(ValidationError, match="items must not be blank"):
        ReviewOutput.model_validate({"summary": "Reviewed.", "findings": [], "limitations": ["Missing.", " \n"]})
    assert ReviewOutput.model_validate({"summary": "Reviewed.", "findings": [], "limitations": []}).limitations == []


def test_output_schema_uses_the_supplied_frozen_contract() -> None:
    content = evidence_anchor_contract_content()
    content["source_line"]["matching_rule"] = "frozen source match"
    contract = EvidenceAnchorContract.model_validate(content)

    evidence = output_schema("review", contract)["$defs"]["Evidence"]

    assert "frozen source match" in evidence["properties"]["quoted_text"]["description"]


def test_source_map_models_enforce_canonical_paths_and_page_order() -> None:
    digest = "a" * 64
    source_map = EvidenceAnchorMap.model_validate(
        {
            "contract_digest": evidence_anchor_contract_digest(),
            "sources": [
                {
                    "source_path": "sections/methods.tex",
                    "read_path": "sources/sections/methods.tex",
                    "source_digest": digest,
                    "line_count": 12,
                    "text_anchorable": True,
                },
                {
                    "source_path": "figures/result.pdf",
                    "read_path": "sources/figures/result.pdf",
                    "source_digest": digest,
                    "line_count": None,
                    "text_anchorable": False,
                },
            ],
            "compiled_pdf": {
                "source_path": "manuscript.pdf",
                "read_path": "manuscript.pdf",
                "page_count": 2,
                "pages": [
                    {"page": 1, "read_path": "pages/page-0001.png", "page_digest": digest},
                    {"page": 2, "read_path": "pages/page-0002.png", "page_digest": digest},
                ],
            },
        }
    )

    assert source_map.compiled_pdf.pages[-1].read_path == "pages/page-0002.png"
    invalid = source_map.model_dump(mode="json")
    invalid["compiled_pdf"]["pages"][1]["read_path"] = "pages/page-2.png"
    with pytest.raises(ValidationError):
        EvidenceAnchorMap.model_validate(invalid)


def test_review_output_schema_forbids_unknown_fields() -> None:
    payload = {
        "summary": "No findings.",
        "findings": [],
        "limitations": [],
        "unexpected": True,
    }
    with pytest.raises(ValidationError):
        ReviewOutput.model_validate(payload)
    assert output_schema("review")["additionalProperties"] is False


def _judged_check(**changes):
    page = {"source_path": "manuscript.pdf", "page": 1}
    check = {
        "claim_index": 0,
        "evidence": [page],
        "critical_question": "Do the tabulated errors stay within 0.1 eV?",
        "countercheck": "Recomputed the error column.",
        "assessment": "supported",
        "finding_indices": [],
        "stated_scope": "All tabulated molecules.",
        "check_type": "reporting_consistency",
        "question_answer": "yes",
        "exceptions": [],
    }
    return {**check, **changes}


RECOMPUTATION = {
    "inputs": ["E_ref = -1.20 eV (Table 1)", "E_calc = -1.12 eV (Table 1)"],
    "calculation": "|E_calc - E_ref|",
    "result": "0.08 eV",
    "reported": "below 0.1 eV",
    "outcome": "matches",
}


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"assessment": "unresolved", "question_answer": "partly", "exceptions": ["One molecule exceeds it."]},
        {"assessment": "unresolved", "question_answer": "not_checkable"},
        {"check_type": "recomputation", "recomputation": RECOMPUTATION},
        {"assessment": "finding", "finding_indices": [0], "question_answer": "no"},
    ],
)
def test_claim_judgments_accept_consistent_answers(changes) -> None:
    check = LinkedClaimCheck.model_validate(_judged_check(**changes))
    assert check.assessment == changes.get("assessment", "supported")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"question_answer": "partly", "exceptions": ["One molecule exceeds it."]}, 'goes with question_answer "yes"'),
        ({"assessment": "unresolved", "question_answer": "partly"}, '"partly" lists at least one exception'),
        ({"assessment": "unresolved"}, 'goes with question_answer "yes"'),
        ({"assessment": "finding", "finding_indices": [0]}, 'goes with question_answer "yes"'),
        ({"exceptions": ["One molecule exceeds it."]}, "lists no exceptions"),
        ({"assessment": "finding", "finding_indices": [0], "question_answer": "not_checkable"}, "not_checkable"),
        ({"check_type": "recomputation"}, "recomputation is recorded exactly"),
        ({"recomputation": RECOMPUTATION}, "recomputation is recorded exactly"),
        (
            {"check_type": "recomputation", "recomputation": {**RECOMPUTATION, "outcome": "differs"}},
            "recomputation that differs",
        ),
        (
            {
                "assessment": "finding",
                "finding_indices": [0],
                "question_answer": "no",
                "check_type": "recomputation",
                "recomputation": RECOMPUTATION,
            },
            'one that matches cannot answer "no"',
        ),
    ],
)
def test_claim_judgments_reject_contradictory_answers(changes, message) -> None:
    with pytest.raises(ValidationError, match=message):
        LinkedClaimCheck.model_validate(_judged_check(**changes))


def test_scientific_review_schema_requires_claim_judgments() -> None:
    check = output_schema("scientific_review")["$defs"]["LinkedClaimCheck"]
    assert {"claim_index", "stated_scope", "check_type", "question_answer", "exceptions"} <= set(check["required"])
    assert "recomputation" not in check["required"]
    assert not {"claim", "claim_anchor"} & set(check["properties"])

import io

from pydantic import ValidationError
import pytest

from scriptorium.schemas import (
    DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    EvidenceAnchorContract,
    evidence_anchor_contract_content,
)

CASES = [
    "",
    "one",
    "one\n",
    "one\ntwo",
    "one\r\ntwo\rthree\n",
    "alpha\fbeta\vgamma\x1c\x1d\x1e\x85  end\n",
    "\n\n",
]


@pytest.mark.parametrize("text", CASES)
def test_default_contract_numbers_lines_like_universal_newline_retrieval(text):
    with io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8") as stream:
        retrieved = [line.removesuffix("\n") for line in stream]
    assert DEFAULT_EVIDENCE_ANCHOR_CONTRACT.split_lines(text) == retrieved
    assert "".join(DEFAULT_EVIDENCE_ANCHOR_CONTRACT.split_lines(text, keepends=True)) == text


def test_terminators_are_required_frozen_content():
    content = evidence_anchor_contract_content()

    assert content["line_terminators"] == ["\r\n", "\r", "\n"]
    without = {key: value for key, value in content.items() if key != "line_terminators"}
    with pytest.raises(ValidationError, match="line_terminators"):
        EvidenceAnchorContract.model_validate(without)
    with pytest.raises(ValidationError, match="line terminators"):
        EvidenceAnchorContract.model_validate({**content, "line_terminators": ["\n", "\f"]})

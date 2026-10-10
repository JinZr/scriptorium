import asyncio

from scriptorium.domain import AgentRole
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, review_finding, submit

_QUOTE = "\\begin{document}\nThe result is teh clear."


def _line_finding(review):
    finding = review_finding(review)
    finding["evidence"] = [{**finding["evidence"][0], "start_line": 2, "end_line": 3, "quoted_text": _QUOTE}]
    return finding


def _page_finding(review):
    finding = review_finding(review)
    finding.pop("affected_claim", None)
    finding.update(
        category="figure",
        severity="minor",
        title="Rendered page lacks a caption",
        claim="The rendered page shows the result without a caption.",
        explanation="A reader of the PDF cannot tell which result the page reports.\n\n## Patches",
        suggested_action="Add a caption naming the result.",
        confidence=0.6,
        evidence=[{"source_path": "manuscript.pdf", "page": 1}],
    )
    return finding


def test_markdown_report_renders_every_finding_field_and_evidence_anchor(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.COPYEDIT)
        line_finding, page_finding = _line_finding(review), _page_finding(review)
        submit(service, review, {"summary": "Reviewed.", "findings": [line_finding, page_finding]})
        stored = service.list_findings(run.id)
        markdown = service.render_report(run.id, "markdown")

    section = markdown.split("\n## Findings\n")[1].split("\n## ")[0]
    lines = [line for line in section.splitlines() if line]
    ids = {finding.title: finding.id for finding in stored}
    expected = [f"- `{ids['Typo obscures the claim']}` — major / pending: Typo obscures the claim"]
    expected.append("  - category: clarity; role: copyedit; confidence: 0.99")
    expected.append("  - claim: The main result sentence contains a typo.")
    if "affected_claim" in line_finding:
        expected.append(f"  - affected claim: {line_finding['affected_claim']}")
    if "consequence" in line_finding:
        expected.append(f"  - consequence: {line_finding['consequence']}")
    expected.append("  - explanation: The typo makes the result sentence harder to read.")
    expected.append("  - suggested action: Replace the sentence with the corrected wording.")
    expected.append("  - evidence: `main.tex:2-3`: `\\begin{document} ⏎ The result is teh clear.`")
    expected.append(f"- `{ids['Rendered page lacks a caption']}` — minor / pending: Rendered page lacks a caption")
    expected.append("  - category: figure; role: copyedit; confidence: 0.6")
    expected.append("  - claim: The rendered page shows the result without a caption.")
    if "consequence" in page_finding:
        expected.append(f"  - consequence: {page_finding['consequence']}")
    # A continuation line never reaches column zero, so "## Patches" cannot become a heading.
    expected.append("  - explanation: A reader of the PDF cannot tell which result the page reports. ⏎  ⏎ ## Patches")
    expected.append("  - suggested action: Add a caption naming the result.")
    expected.append("  - evidence: `manuscript.pdf:page 1`")
    assert lines == expected


def test_markdown_report_without_findings_lists_none(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        markdown = service.render_report(run.id, "markdown")

    assert markdown.split("\n## Findings\n")[1].startswith("\n- None\n")


def _finding_record(**overrides):
    finding = {
        "id": "finding-1",
        "severity": "minor",
        "status": "pending",
        "title": "Quote",
        "category": "style",
        "role": "copyedit",
        "confidence": 0.5,
        "claim": "Line one.\r\nLine two.\rLine three.",
        "explanation": "Form feed\x0cand U+2028\u2028inside a line.",
        "suggested_action": "Trailing break keeps its mark.\n",
        "evidence": [
            {
                "source_path": "main.tex",
                "start_line": 3,
                "end_line": 3,
                "source_digest": "0" * 64,
                "quoted_text": "The result\x0cis\u2028clear.\n",
            }
        ],
    }
    return {"finding": {**finding, **overrides}, "decisions": []}


def test_markdown_findings_mark_only_the_frozen_contract_line_terminators():
    lines = ScriptoriumService._markdown_findings([_finding_record()], ("\r\n", "\r", "\n"))

    assert lines[3:] == [
        "- `finding-1` — minor / pending: Quote",
        "  - category: style; role: copyedit; confidence: 0.5",
        "  - claim: Line one. ⏎ Line two. ⏎ Line three.",
        "  - explanation: Form feed\x0cand U+2028\u2028inside a line.",
        "  - suggested action: Trailing break keeps its mark. ⏎",
        "  - evidence: `main.tex:3-3`: `The result\x0cis\u2028clear. ⏎`",
    ]


def test_markdown_findings_mark_every_line_break_of_a_legacy_contract():
    # Contracts frozen without line terminators numbered lines with str.splitlines().
    lines = ScriptoriumService._markdown_findings([_finding_record()], None)

    assert lines[5:] == [
        "  - claim: Line one. ⏎ Line two. ⏎ Line three.",
        "  - explanation: Form feed ⏎ and U+2028 ⏎ inside a line.",
        "  - suggested action: Trailing break keeps its mark. ⏎",
        "  - evidence: `main.tex:3-3`: `The result ⏎ is ⏎ clear. ⏎`",
    ]


def test_markdown_findings_keep_a_multiline_category_and_historical_anchors_on_their_items():
    # A historical schema could accept a page beside a validated line range; the range is the anchor. Older
    # page anchors were stored with null line keys and stay page anchors.
    record = _finding_record(
        category="style\n\n## Patches",
        suggested_action="A literal marker ⏎ ",
        evidence=[
            {"source_path": "main.tex", "start_line": 3, "end_line": 4, "page": 1, "quoted_text": "x"},
            {"source_path": "manuscript.pdf", "page": 2, "start_line": None, "end_line": None, "quoted_text": None},
        ],
    )
    lines = ScriptoriumService._markdown_findings([record], ("\r\n", "\r", "\n"))

    assert lines[4] == "  - category: style ⏎  ⏎ ## Patches; role: copyedit; confidence: 0.5"
    assert lines[-3] == "  - suggested action: A literal marker ⏎ "
    assert lines[-2:] == ["  - evidence: `main.tex:3-4`: `x`", "  - evidence: `manuscript.pdf:page 2`"]


def test_markdown_findings_quote_evidence_as_literal_code_spans():
    quotes = [
        "a\\_b $x$ <!-- hidden -->",
        "tick ` and `` runs",
        "`leading and trailing`",
        "line one\nline two",
        " x ",
        "  ",
    ]
    evidence = [{"source_path": "main.tex", "start_line": 1, "end_line": 1, "quoted_text": quote} for quote in quotes]
    lines = ScriptoriumService._markdown_findings([_finding_record(evidence=evidence)], ("\r\n", "\r", "\n"))

    assert lines[-6:] == [
        "  - evidence: `main.tex:1-1`: `a\\_b $x$ <!-- hidden -->`",
        "  - evidence: `main.tex:1-1`: ```tick ` and `` runs```",
        "  - evidence: `main.tex:1-1`: `` `leading and trailing` ``",
        "  - evidence: `main.tex:1-1`: `line one ⏎ line two`",
        "  - evidence: `main.tex:1-1`: `  x  `",
        "  - evidence: `main.tex:1-1`: `  `",
    ]

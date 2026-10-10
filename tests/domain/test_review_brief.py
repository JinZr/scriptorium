from pydantic import ValidationError
import pytest

from scriptorium.domain import ReviewStage, VenueFamily
from scriptorium.schemas import SEVERITY_RUBRIC, ReviewBrief, render_review_brief

FULL_BRIEF = {
    "venue_family": "ml_conference",
    "venue": "NeurIPS 2026",
    "stage": "presubmission",
    "priority_claims": ["The method beats the baseline on all three benchmarks."],
    "known_weaknesses": ["Only one random seed for the ablation."],
    "prior_reviews": "Reviewer 2 asked for a stronger baseline.",
    "ignore": ["Checklist answers"],
    "severity_notes": "Checklist items were all waived last time.",
    "recorded_by": "codex session-1",
}


def test_a_minimal_brief_defaults_every_optional_field() -> None:
    brief = ReviewBrief.model_validate({"venue_family": "nature_family", "stage": "internal_draft"})

    assert brief.venue_family == VenueFamily.NATURE_FAMILY
    assert brief.stage == ReviewStage.INTERNAL_DRAFT
    assert brief.model_dump(mode="json") == {
        "venue_family": "nature_family",
        "venue": None,
        "stage": "internal_draft",
        "priority_claims": [],
        "known_weaknesses": [],
        "prior_reviews": None,
        "ignore": [],
        "severity_notes": None,
        "recorded_by": None,
    }


def test_a_full_brief_round_trips_through_json() -> None:
    brief = ReviewBrief.model_validate(FULL_BRIEF)

    assert ReviewBrief.model_validate_json(brief.model_dump_json()).model_dump(mode="json") == FULL_BRIEF


@pytest.mark.parametrize(
    "change",
    [
        {"reviewer_model": "x"},
        {"venue_family": "workshop"},
        {"stage": "submitted"},
        {"priority_claims": ["   "]},
        {"priority_claims": ["claim"] * 11},
        {"ignore": ["x" * 501]},
        {"venue": ""},
        {"prior_reviews": " \n "},
        {"known_weaknesses": "one string"},
    ],
)
def test_invalid_briefs_are_rejected(change) -> None:
    with pytest.raises(ValidationError):
        ReviewBrief.model_validate({**FULL_BRIEF, **change})


@pytest.mark.parametrize("missing", ["venue_family", "stage"])
def test_venue_family_and_stage_are_required(missing) -> None:
    with pytest.raises(ValidationError):
        ReviewBrief.model_validate({key: value for key, value in FULL_BRIEF.items() if key != missing})


def test_rendering_is_deterministic_and_names_the_venue_and_stage() -> None:
    rendered = render_review_brief(ReviewBrief.model_validate(FULL_BRIEF))

    assert rendered == render_review_brief(ReviewBrief.model_validate(dict(reversed(FULL_BRIEF.items()))))
    assert "- Venue family: ml_conference (a machine-learning conference)" in rendered
    assert "- Venue: NeurIPS 2026" in rendered
    assert "- Stage: presubmission (a manuscript about to be submitted)" in rendered
    assert "  - The method beats the baseline on all three benchmarks." in rendered
    assert "Reviewer 2 asked for a stronger baseline." in rendered
    assert "codex session-1" not in rendered
    assert SEVERITY_RUBRIC not in rendered
    assert "Severity rubric" not in rendered


def test_rendering_omits_sections_the_authors_left_empty() -> None:
    rendered = render_review_brief(ReviewBrief.model_validate({"venue_family": "other", "stage": "camera_ready"}))

    assert rendered.splitlines()[1:] == [
        "- Venue family: other (another venue)",
        "- Stage: camera_ready (an accepted paper being finalized)",
    ]

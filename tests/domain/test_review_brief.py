import hashlib
import json

import pytest

from scriptorium.brief import BRIEF_LIMIT, parse_brief, read_brief, render_brief
from scriptorium.domain import ReviewStage, VenueFamily
from scriptorium.errors import ConfigurationError
from scriptorium.schemas import SEVERITY_RUBRIC

BODY = """## Claims the authors most want checked
- The gain on benchmark one holds across seeds.

## Out of scope this round
- Related work."""
FULL_BRIEF = f"""---
venue_family: ml_conference
venue: NeurIPS 2026
stage: presubmission
recorded_by: codex session-1
---
{BODY}
"""
MINIMAL_BRIEF = "---\nvenue_family: other\nstage: camera_ready\n---\n"


def _parse(text: str):
    return parse_brief(text.encode("utf-8"))


def _problems(text: str) -> list[str]:
    brief, problems = _parse(text)
    assert brief is None
    return problems


def test_a_full_brief_parses_its_front_matter_and_keeps_its_body() -> None:
    brief, problems = _parse(FULL_BRIEF)

    assert problems == []
    assert brief.venue_family == VenueFamily.ML_CONFERENCE
    assert brief.stage == ReviewStage.PRESUBMISSION
    assert (brief.venue, brief.recorded_by, brief.body) == ("NeurIPS 2026", "codex session-1", BODY)
    assert brief.data == FULL_BRIEF.encode("utf-8")
    assert brief.digest == hashlib.sha256(brief.data).hexdigest()
    assert brief.fields() == {"venue_family": "ml_conference", "venue": "NeurIPS 2026", "stage": "presubmission"}


def test_a_minimal_brief_has_no_venue_and_a_blank_body() -> None:
    brief, problems = _parse(MINIMAL_BRIEF)

    assert problems == []
    assert (brief.venue, brief.recorded_by, brief.body) == (None, None, "")
    assert brief.fields() == {"venue_family": "other", "venue": None, "stage": "camera_ready"}


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("venue_family: other\nstage: camera_ready\n", "a brief starts with a line ---"),
        ("---\nvenue_family: other\nstage: camera_ready\n", "a brief starts with a line ---"),
        (json.dumps({"venue_family": "other", "stage": "camera_ready"}), "a brief starts with a line ---"),
        ("---\nstage: camera_ready\n---\n", "venue_family is required: one of ml_conference, nature_family, other"),
        ("---\nvenue_family: other\n---\n", "stage is required"),
        ("---\nvenue_family: other\nstage: camera_ready\nignore: x\n---\n", "line 4: unknown key 'ignore'"),
        ("---\nvenue_family: other\nvenue_family: other\nstage: camera_ready\n---\n", "line 3: venue_family appears"),
        ("---\nvenue_family: workshop\nstage: camera_ready\n---\n", "venue_family must be one of"),
        ("---\nvenue_family: other\nstage: submitted\n---\n", "stage must be one of internal_draft"),
        ("---\nvenue_family: other\nstage: camera_ready\nvenue:   \n---\n", "venue must be non-blank"),
        (f"---\nvenue_family: other\nstage: camera_ready\nvenue: {'V' * 201}\n---\n", "at most 200 characters"),
        ("---\nvenue_family: other\nstage: camera_ready\nrecorded_by:\n---\n", "recorded_by must be non-blank"),
        ("---\nvenue_family: other\nstage camera_ready\n---\n", "line 3: expected 'key: value'"),
    ],
)
def test_invalid_briefs_name_the_problem(text: str, problem: str) -> None:
    assert any(problem in item for item in _problems(text)), _problems(text)


def test_every_problem_is_named_at_once() -> None:
    problems = _problems("---\nvenue_family: workshop\nseverity_notes: x\nvenue:\n---\n")

    assert len(problems) == 4
    assert any("unknown key 'severity_notes'" in item for item in problems)
    assert any("stage is required" in item for item in problems)


def test_a_venue_of_exactly_200_characters_is_accepted() -> None:
    brief, _ = _parse(f"---\nvenue_family: other\nstage: camera_ready\nvenue: {'V' * 200}\n---\n")

    assert brief.venue == "V" * 200


def test_a_brief_that_is_not_utf8_is_invalid() -> None:
    assert parse_brief(b"---\nvenue_family: other\nstage: camera_ready\n---\n\xff") == (None, ["the file is not UTF-8"])


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (None, "invalid review brief: the file is missing"),
        (b"\xff", "invalid review brief: the file is not UTF-8"),
        (MINIMAL_BRIEF.encode() + b" " * BRIEF_LIMIT, "invalid review brief: the file exceeds 64 KB"),
        (b'{"venue_family": "other", "stage": "camera_ready"}', "invalid review brief: a brief starts with"),
    ],
)
def test_read_brief_fails_with_a_configuration_error(tmp_path, contents, message) -> None:
    path = tmp_path / "brief.md"
    if contents is not None:
        path.write_bytes(contents)

    with pytest.raises(ConfigurationError, match=message):
        read_brief(path)


def test_read_brief_refuses_a_directory_and_accepts_a_file_at_the_limit(tmp_path) -> None:
    with pytest.raises(ConfigurationError, match="not a regular file"):
        read_brief(tmp_path)
    path = tmp_path / "brief.md"
    data = MINIMAL_BRIEF.encode() + b"x" * (BRIEF_LIMIT - len(MINIMAL_BRIEF))
    path.write_bytes(data)

    assert read_brief(path).data == data


def test_rendering_is_deterministic_and_names_the_venue_and_stage() -> None:
    rendered = render_brief(_parse(FULL_BRIEF)[0])

    assert rendered == render_brief(_parse(FULL_BRIEF)[0])
    assert rendered.startswith("Review brief, recorded with the authors before this run and frozen with it.")
    assert "- Venue family: ml_conference (a machine-learning conference)" in rendered
    assert "- Venue: NeurIPS 2026" in rendered
    assert "- Stage: presubmission (a manuscript about to be submitted)" in rendered
    assert "report it only if it meets the blocker definition" in rendered
    assert f"BEGIN AUTHORS' NOTES\n{BODY}\nEND AUTHORS' NOTES" in rendered
    assert "not evidence" in rendered
    assert "codex session-1" not in rendered
    assert SEVERITY_RUBRIC not in rendered and "Severity rubric" not in rendered


def test_rendering_leaves_out_a_blank_body_and_a_missing_venue() -> None:
    rendered = render_brief(_parse(MINIMAL_BRIEF + "\n  \n")[0])

    assert rendered.splitlines()[1:] == [
        "- Venue family: other (another venue)",
        "- Stage: camera_ready (an accepted paper being finalized)",
        "Where the brief names something as out of scope for this round, report it only if it meets the blocker "
        "definition.",
    ]

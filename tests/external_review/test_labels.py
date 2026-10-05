import json

import pytest

from egs.external_review.collect import collect
from egs.external_review.files import read_json, verify_seal
from egs.external_review.judge import blind, summarize
from egs.external_review.labels import score, seal_labels
from scriptorium.service import ScriptoriumService

from ._support import project, start, submit

LABELS = {
    "label_set": "held-out",
    "annotators": ["annotator-a"],
    "cases": [
        {
            "case": "paper",
            "problems": [
                {"id": "P1", "kind": "planted", "summary": "Typo in the result.", "locations": ["main.tex:3"]},
                {"id": "P2", "kind": "documented", "summary": "Unsupported scope.", "locations": ["main.tex:4"]},
            ],
        },
        {"case": "paper-fixed", "control_of": "paper", "corrected": ["P1"]},
    ],
}


def save(path, value):
    path.write_text(json.dumps(value))
    return path


@pytest.fixture(scope="module")
def packet(tmp_path_factory):
    directory = tmp_path_factory.mktemp("held-out")
    root = project(directory)
    collections = []
    for case, trial, computation, findings in (
        ("paper", "with-tools", "allowed", True),
        ("paper", "without-tools", "denied", False),
        ("paper-fixed", "with-tools", "allowed", True),
    ):
        run_id, task_id = start(root)
        with ScriptoriumService(root) as service:
            submit(service, task_id, session=f"{case}-{trial}", findings=findings)
        output = directory / f"{case}-{trial}"
        collections.append(collect(root, run_id, output, case, trial, host_computation=computation))
    return blind(collections, directory / "packet", 5)


def matches(packet, labels, problems=("P1",)):
    return {
        "packet_digest": verify_seal(packet / "public"),
        "labels_digest": verify_seal(labels),
        "matchers": ["annotator-a"],
        "matches": [
            {"candidate_id": candidate_id, "problems": list(problems)}
            for candidate_id in read_json(packet / "mapping.json")["candidates"]
        ],
    }


def test_score_reports_labeled_recall_control_false_alarms_and_host_computation(packet, tmp_path):
    labels = seal_labels(save(tmp_path / "labels.json", LABELS), tmp_path / "labels")
    result = score(packet, labels, save(tmp_path / "matches.json", matches(packet, labels)), tmp_path / "score")
    verify_seal(tmp_path / "score")
    trials = {(item["case"], item["trial"]): item for item in result["trials"]}
    assert trials["paper", "with-tools"]["found"] == ["P1"]
    assert trials["paper", "with-tools"]["missed"] == ["P2"]
    assert trials["paper", "with-tools"]["found_by_kind"] == {"planted": [1, 1], "documented": [0, 1]}
    assert trials["paper", "without-tools"]["found"] == []
    assert trials["paper", "without-tools"]["host_computation"] == "denied"
    assert trials["paper-fixed", "with-tools"]["false_alarms"] == ["P1"]
    assert result["by_trial"] == [
        {
            "trial": "with-tools",
            "cases": 2,
            "accepted_reviews": 2,
            "host_computation": ["allowed"],
            "labeled_problems_found": [1, 2],
            "labeled_recall": 0.5,
            "control_false_alarms": [1, 1],
        },
        {
            "trial": "without-tools",
            "cases": 1,
            "accepted_reviews": 1,
            "host_computation": ["denied"],
            "labeled_problems_found": [0, 2],
            "labeled_recall": 0.0,
            "control_false_alarms": [0, 0],
        },
    ]
    assert read_json(tmp_path / "score/labels.json") == LABELS


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda value: value["cases"][0].update(problems=[]), "lists unique problems"),
        (lambda value: value["cases"][1].update(corrected=["P9"]), "unknown problem"),
        (lambda value: value["cases"][1].update(control_of="paper-fixed"), "must correct a labeled case"),
        (lambda value: value["cases"].append(value["cases"][0]), "labeled once"),
        (lambda value: value.update(annotators=[]), "annotators"),
    ],
)
def test_invalid_labels_are_not_sealed(tmp_path, change, message):
    value = json.loads(json.dumps(LABELS))
    change(value)
    with pytest.raises(ValueError, match=message):
        seal_labels(save(tmp_path / "labels.json", value), tmp_path / "labels")
    assert not (tmp_path / "labels").exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda value: value["matches"].pop(), "every candidate exactly once"),
        (lambda value: value.update(labels_digest="0" * 64), "different packet or label set"),
        (lambda value: value["matches"][0].update(problems=["P2"]), "not labeled for paper-fixed"),
        (lambda value: value["matches"][0].update(problems=["P1", "P1"]), "repeats a problem"),
    ],
)
def test_score_rejects_unbound_or_invalid_matches(packet, tmp_path, change, message):
    labels = seal_labels(save(tmp_path / "labels.json", LABELS), tmp_path / "labels")
    value = matches(packet, labels)
    change(value)
    with pytest.raises(ValueError, match=message):
        score(packet, labels, save(tmp_path / "matches.json", value), tmp_path / "score")
    assert not (tmp_path / "score").exists()


def test_score_requires_labels_for_every_trial_case(packet, tmp_path):
    value = json.loads(json.dumps(LABELS))
    value["cases"].pop()
    labels = seal_labels(save(tmp_path / "labels.json", value), tmp_path / "labels")
    with pytest.raises(ValueError, match="no human labels: paper-fixed"):
        score(packet, labels, save(tmp_path / "matches.json", matches(packet, labels, ())), tmp_path / "score")


def test_planned_host_computation_is_a_compared_input(packet, tmp_path):
    comparisons = {item["case"]: item for item in summarize(packet, [], tmp_path / "summary")["input_comparisons"]}
    assert comparisons["paper"]["varying_inputs"] == ["host_computation"]

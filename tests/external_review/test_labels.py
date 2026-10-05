import json
import subprocess

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
    "trials": ["with-tools", "without-tools"],
    "cases": [
        {
            "case": "paper",
            "tree_sha": "0" * 40,
            "problems": [
                {"id": "P1", "kind": "planted", "summary": "Typo in the result.", "locations": ["main.tex:3"]},
                {"id": "P2", "kind": "documented", "summary": "Unsupported scope.", "locations": ["main.tex:4"]},
            ],
        },
        {"case": "paper-fixed", "tree_sha": "0" * 40, "control_of": "paper", "corrected": ["P1"]},
    ],
}


def save(path, value):
    path.write_text(json.dumps(value))
    return path


def planned(root, value=LABELS):
    """Label every case with the project's committed tree, as an operator does before any trial."""
    tree = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], check=True, capture_output=True, text=True
    ).stdout.strip()
    value = json.loads(json.dumps(value))
    for item in value["cases"]:
        item["tree_sha"] = tree
    return value


def bound(packet, value=LABELS):
    """Bind each labeled case to the tree its trials were collected from."""
    trees = {trial["case"]: trial["comparison"]["tree"] for trial in read_json(packet / "mapping.json")["trials"]}
    value = json.loads(json.dumps(value))
    for item in value["cases"]:
        item["tree_sha"] = trees.get(item["case"], item["tree_sha"])
    return value


@pytest.fixture(scope="module")
def packet(tmp_path_factory):
    directory = tmp_path_factory.mktemp("held-out")
    root = project(directory)
    labels = seal_labels(save(directory / "labels.json", planned(root)), directory / "labels")
    collections = []
    for case, trial, computation, findings in (
        ("paper", "with-tools", "allowed", True),
        ("paper", "without-tools", "denied", False),
        ("paper-fixed", "with-tools", "allowed", True),
        ("paper-fixed", "without-tools", "denied", False),
    ):
        run_id, task_id = start(root)
        before = directory / f"{case}-{trial}-before"
        collect(root, run_id, before, case, trial, host_computation=computation, labels=labels)
        with ScriptoriumService(root) as service:
            submit(service, task_id, session=f"{case}-{trial}", findings=findings)
        output = directory / f"{case}-{trial}"
        collections.append(collect(root, run_id, output, case, trial, before, host_computation=computation))
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
    labels = seal_labels(save(tmp_path / "labels.json", bound(packet)), tmp_path / "labels")
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
            "cases": 2,
            "accepted_reviews": 2,
            "host_computation": ["denied"],
            "labeled_problems_found": [0, 2],
            "labeled_recall": 0.0,
            "control_false_alarms": [0, 1],
        },
    ]
    assert read_json(tmp_path / "score/labels.json") == bound(packet)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda value: value["cases"][0].update(problems=[]), "lists unique problems"),
        (lambda value: value["cases"][1].update(corrected=["P9"]), "unknown problem"),
        (lambda value: value["cases"][1].update(control_of="paper-fixed"), "must correct a labeled case"),
        (lambda value: value["cases"].append(value["cases"][0]), "labeled once"),
        (lambda value: value.update(annotators=[]), "annotators"),
        (lambda value: value["cases"][0].update(tree_sha="HEAD"), "tree_sha"),
        (lambda value: value.update(trials=[]), "trials"),
        (lambda value: value.update(trials=["with-tools", "with-tools"]), "planned trial is named once"),
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
    labels = seal_labels(save(tmp_path / "labels.json", bound(packet)), tmp_path / "labels")
    value = matches(packet, labels)
    change(value)
    with pytest.raises(ValueError, match=message):
        score(packet, labels, save(tmp_path / "matches.json", value), tmp_path / "score")
    assert not (tmp_path / "score").exists()


def test_score_requires_labels_for_every_trial_case(packet, tmp_path):
    value = bound(packet)
    value["cases"].pop()
    labels = seal_labels(save(tmp_path / "labels.json", value), tmp_path / "labels")
    with pytest.raises(ValueError, match="no human labels: paper-fixed"):
        score(packet, labels, save(tmp_path / "matches.json", matches(packet, labels, ())), tmp_path / "score")


def test_planned_host_computation_is_a_compared_input(packet, tmp_path):
    comparisons = {item["case"]: item for item in summarize(packet, [], tmp_path / "summary")["input_comparisons"]}
    assert comparisons["paper"]["varying_inputs"] == ["host_computation"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda value: value["cases"][0].update(tree_sha="1" * 40), "not collected from labeled tree"),
        (
            lambda value: value["cases"].append({**value["cases"][0], "case": "paper-2"}),
            "Trial with-tools is missing labeled cases: paper-2",
        ),
        # A packet that drops a whole planned arm cannot report only the arms that remain.
        (lambda value: value["trials"].append("with-host-search"), "Planned trials are missing: with-host-search"),
        (lambda value: value.update(trials=["with-tools"]), "Trial without-tools is not planned"),
        # Labels written after the reviews are seen were never bound to the trial baselines.
        (lambda value: value.update(annotators=["annotator-b"]), "not bound to these labels before review"),
    ],
)
def test_score_rejects_labels_for_other_trees_or_uncollected_cases(packet, tmp_path, change, message):
    value = bound(packet)
    change(value)
    labels = seal_labels(save(tmp_path / "labels.json", value), tmp_path / "labels")
    with pytest.raises(ValueError, match=message):
        score(packet, labels, save(tmp_path / "matches.json", matches(packet, labels, ())), tmp_path / "score")
    assert not (tmp_path / "score").exists()


def test_labels_bind_to_a_planned_baseline_before_the_first_attempt(tmp_path):
    root = project(tmp_path)
    run_id, task_id = start(root)
    labels = seal_labels(save(tmp_path / "labels.json", planned(root)), tmp_path / "labels")
    with pytest.raises(ValueError, match="Trial other is not planned"):
        collect(root, run_id, tmp_path / "unplanned", "paper", "other", labels=labels)
    before = collect(root, run_id, tmp_path / "before", "paper", "with-tools", labels=labels)
    assert read_json(before / "collection.json")["labels_digest"] == verify_seal(labels)
    with ScriptoriumService(root) as service:
        submit(service, task_id)
    with pytest.raises(ValueError, match="only before the first attempt"):
        collect(root, run_id, tmp_path / "late", "paper", "with-tools", labels=labels)
    other = save(tmp_path / "other.json", planned(root, {**LABELS, "annotators": ["annotator-b"]}))
    with pytest.raises(ValueError, match="Baseline was not bound to these labels"):
        collect(
            root, run_id, tmp_path / "after", "paper", "with-tools", before, labels=seal_labels(other, tmp_path / "o")
        )
    after = collect(root, run_id, tmp_path / "after", "paper", "with-tools", before, labels=labels)
    assert read_json(after / "collection.json")["labels_digest"] == verify_seal(labels)

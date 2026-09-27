import asyncio
import json
import shutil
import subprocess

import pytest

from egs.external_review import judge as evaluator
from egs.external_review.collect import collect
from egs.external_review.files import read_json, seal, verify_seal
from egs.external_review.judge import blind, summarize
from scriptorium.service import ScriptoriumService

from ._support import project, start, submit


@pytest.fixture(scope="module")
def collections(tmp_path_factory):
    directory = tmp_path_factory.mktemp("external-evaluation")
    root = project(directory)
    paths = []
    for number in range(3):
        run_id, task_id = start(root)
        with ScriptoriumService(root) as service:
            if number < 2:
                submit(service, task_id, model=f"model-{number}", session=f"review-{number}")
            else:
                claim = service.claim_task(task_id, "claude_code", "model-failed", "high", "review-failed", "host")
                asyncio.run(service.submit_task(claim["attempt"].id, claim["input_digest"], "invalid"))
        paths.append(collect(root, run_id, directory / f"trial-{number}", "paper", f"trial-{number}"))
    return paths


def judgment(packet, *, model="judge-a", verdict="supported_concern"):
    return {
        "packet_digest": verify_seal(packet / "public"),
        "client": "codex",
        "model": model,
        "effort": "low",
        "session_id": f"session-{model}",
        "session_source": "host",
        "cost_usd": None,
        "judgments": [
            {
                "candidate_id": candidate["candidate_id"],
                "verdict": verdict,
                "significance": "minor",
                "evidence_locations": ["sources/main.tex:3"],
                "counterevidence": "Checked context.",
                "rationale": "The text supports this limited concern.",
                "duplicates": [],
            }
            for candidate in read_json(packet / "public/input.json")["candidates"]
        ],
        "limitations": ["Model assessment only."],
    }


def save(path, value):
    path.write_text(json.dumps(value))
    return path


def test_blind_packet_hides_provenance_combines_exact_duplicates_and_retains_failed_trial(collections, tmp_path):
    packet = blind(collections, tmp_path / "packet", 13)
    verify_seal(packet)
    public = read_json(packet / "public/input.json")
    assert len(public["candidates"]) == 1
    candidate = public["candidates"][0]
    assert set(candidate) == {
        "candidate_id",
        "material",
        "title",
        "claim",
        "evidence",
        "explanation",
        "suggested_action",
    }
    for forbidden in ("model-0", "model-1", "review-0", "review-1", '"severity"', '"confidence"', '"trial"'):
        assert forbidden not in (packet / "public/input.json").read_text()
    assert not (packet / "public/mapping.json").exists()
    mapping = read_json(packet / "mapping.json")
    assert len(mapping["candidates"][candidate["candidate_id"]]) == 2
    assert len(mapping["trials"]) == 3
    copied = packet / "public/material" / candidate["material"] / "sources/main.tex"
    assert copied.read_bytes() == (collections[0] / "bundle/sources/main.tex").read_bytes()
    result = summarize(packet, [], tmp_path / "not-judged")
    assert result["scientific_status"] == "not_judged"
    assert result["trials"][2]["candidate_count"] == 0
    assert result["trials"][2]["workflow"]["attempt_status_counts"] == {"failed": 1}
    assert result["trials"][2]["scientific_assessments"] == []
    assert result["input_comparisons"] == [{"case": "paper", "trial_count": 3, "varying_inputs": []}]


def test_multiple_judges_preserve_disagreement_and_original_outputs(collections, tmp_path):
    packet = blind(collections, tmp_path / "packet", 13)
    first = save(tmp_path / "a.json", judgment(packet))
    second = save(tmp_path / "b.json", judgment(packet, model="judge-b", verdict="not_supported"))
    result = summarize(packet, [first, second], tmp_path / "results")
    assert result["scientific_status"] == "model_judgments"
    assert result["disagreements"] == ["C0001"]
    assert result["trials"][0]["scientific_assessments"][0]["verdicts"] == {"supported_concern": 1}
    assert result["trials"][0]["scientific_assessments"][1]["verdicts"] == {"not_supported": 1}
    assert all(item["cost_usd"] is None for item in result["judges"])
    assert not result["trials"][2]["has_accepted_review"]
    assert result["trials"][2]["scientific_assessments"] == []
    assert (tmp_path / "results/judge-001.json").read_bytes() == first.read_bytes()
    assert (tmp_path / "results/judge-002.json").read_bytes() == second.read_bytes()
    verify_seal(tmp_path / "results")


def test_duplicate_event_origins_preserve_continuation_and_other_role(tmp_path):
    root = project(tmp_path)
    configuration = root / "scriptorium.toml"
    configuration.write_text(configuration.read_text().replace('["copyedit"]', '["copyedit", "consistency"]'))
    subprocess.run(["git", "-C", str(root), "add", "scriptorium.toml"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "Two roles",
        ],
        check=True,
    )
    run_id, task_id = start(root)
    with ScriptoriumService(root) as service:
        first = submit(service, task_id, completion="partial")
        asyncio.run(service.continue_review(run_id, task_id))
        continued = submit(service, task_id, session="continued")
        other_id = next(item["task"].id for item in service.list_tasks(run_id)["tasks"] if item["task"].id != task_id)
        other = submit(service, other_id, session="other-role")
    collection = collect(root, run_id, tmp_path / "collection", "paper", "multi-role")
    packet = blind([collection], tmp_path / "packet", 13)
    mapping = read_json(packet / "mapping.json")
    assert len(mapping["candidates"]) == 1
    origins = mapping["candidates"]["C0001"]
    assert [origin["attempt_id"] for origin in origins] == [
        context["attempt"].id for context in (first, continued, other)
    ]
    assert [origin["role"] for origin in origins] == ["copyedit", "copyedit", "consistency"]
    events = [
        event for event in read_json(collection / "report.json")["events"] if event["event_type"] == "finding.duplicate"
    ]
    assert [origin["duplicate_event_id"] for origin in origins[1:]] == [event["id"] for event in events]
    assert all("confidence" not in origin and "severity" not in origin for origin in origins[1:])
    assert {origin["finding_id"] for origin in origins} == {origins[0]["finding_id"]}
    result = summarize(packet, [], tmp_path / "results")
    assert result["trials"][0]["candidate_count"] == 1


@pytest.mark.parametrize(
    "bad",
    ["digest", "missing", "duplicate", "unknown", "self_duplicate", "reused_session", "negative_cost", "nan_cost"],
)
def test_invalid_judge_result_cannot_publish_scores(collections, tmp_path, bad):
    packet = blind(collections[:1], tmp_path / "packet", 13)
    value = judgment(packet)
    if bad == "digest":
        value["packet_digest"] = "a" * 64
    elif bad == "missing":
        value["judgments"] = []
    elif bad == "duplicate":
        value["judgments"] *= 2
    elif bad == "unknown":
        value["judgments"][0]["candidate_id"] = "not-in-packet"
    elif bad == "self_duplicate":
        value["judgments"][0]["duplicates"] = ["C0001"]
    elif bad == "reused_session":
        value["session_id"] = "review-0"
    elif bad == "negative_cost":
        value["cost_usd"] = -1
    else:
        value["cost_usd"] = float("nan")
    path = save(tmp_path / "judge.json", value)
    with pytest.raises(ValueError):
        summarize(packet, [path], tmp_path / "results")
    assert not (tmp_path / "results").exists()


def test_repeated_judge_and_repeated_run_are_not_independent_replicates(collections, tmp_path):
    with pytest.raises(ValueError, match="Duplicate case/trial"):
        blind([collections[0], collections[0]], tmp_path / "duplicate", 13)
    packet = blind(collections, tmp_path / "packet", 13)
    path = save(tmp_path / "judge.json", judgment(packet))
    with pytest.raises(ValueError, match="Duplicate judge session"):
        summarize(packet, [path, path], tmp_path / "results")


def test_packet_material_changes_rejected_and_outputs_not_overwritten(collections, tmp_path):
    packet = blind(collections, tmp_path / "packet", 13)
    path = save(tmp_path / "judge.json", judgment(packet))
    with pytest.raises(ValueError, match="already exists"):
        blind(collections, packet, 13)
    material = next((packet / "public/material").glob("*/sources/main.tex"))
    material.write_text("changed")
    with pytest.raises(ValueError, match="Collection has changed"):
        summarize(packet, [path], tmp_path / "results")


@pytest.mark.parametrize("reseal", [False, True])
def test_collection_changed_during_copy_cannot_publish_packet(collections, tmp_path, monkeypatch, reseal):
    collection = tmp_path / "collection"
    shutil.copytree(collections[0], collection)
    original = evaluator.shutil.copytree

    def changing_copy(source, destination, *args, **kwargs):
        if source == collection / "bundle":
            (source / "sources/main.tex").write_text("Changed while preparing packet")
            if reseal:
                (collection / "seal.json").unlink()
                seal(collection)
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(evaluator.shutil, "copytree", changing_copy)
    with pytest.raises(ValueError, match="Collection has changed"):
        blind([collection], tmp_path / "packet", 13)
    assert not (tmp_path / "packet").exists()


@pytest.mark.parametrize("reseal", [False, True])
def test_packet_changed_during_summary_cannot_publish_results(collections, tmp_path, monkeypatch, reseal):
    packet = blind(collections[:1], tmp_path / "packet", 13)
    original = evaluator.read_json

    def changing_read(path):
        value = original(path)
        if path == packet / "mapping.json":
            changed = json.loads(json.dumps(value))
            changed["trials"][0]["trial"] = "changed-during-summary"
            save(path, changed)
            if reseal:
                (packet / "seal.json").unlink()
                seal(packet)
        return value

    monkeypatch.setattr(evaluator, "read_json", changing_read)
    with pytest.raises(ValueError, match="Collection has changed"):
        summarize(packet, [], tmp_path / "results")
    assert not (tmp_path / "results").exists()


def test_summary_binds_private_mapping_when_public_packets_match(collections, tmp_path):
    packets = [
        blind([collection], tmp_path / f"packet-{number}", 13) for number, collection in enumerate(collections[:2])
    ]
    assert verify_seal(packets[0] / "public") == verify_seal(packets[1] / "public")
    results = [summarize(packet, [], tmp_path / f"results-{number}") for number, packet in enumerate(packets)]
    assert results[0]["packet_digest"] == results[1]["packet_digest"]
    assert results[0]["packet_seal_digest"] != results[1]["packet_seal_digest"]
    for packet, result in zip(packets, results):
        assert result["packet_seal_digest"] == verify_seal(packet)


def test_outputs_cannot_be_nested_in_sealed_inputs(collections, tmp_path):
    with pytest.raises(ValueError, match="outside sealed input"):
        blind(collections, collections[0] / "bundle/packet", 13)
    packet = blind(collections, tmp_path / "packet", 13)
    with pytest.raises(ValueError, match="outside sealed input"):
        summarize(packet, [], packet / "results")
    verify_seal(packet)


def test_judge_snapshot_keeps_the_exact_validated_bytes(collections, tmp_path, monkeypatch):
    packet = blind(collections, tmp_path / "packet", 13)
    path = save(tmp_path / "judge.json", judgment(packet))
    expected = path.read_bytes()
    original = evaluator.validate_judge

    def changed_after_validation(*args):
        result = original(*args)
        path.write_text("changed after validation")
        return result

    monkeypatch.setattr(evaluator, "validate_judge", changed_after_validation)
    summarize(packet, [path], tmp_path / "results")
    assert (tmp_path / "results/judge-001.json").read_bytes() == expected


def test_valid_zero_finding_review_does_not_require_invented_candidates(tmp_path):
    root = project(tmp_path)
    run_id, task_id = start(root)
    with ScriptoriumService(root) as service:
        submit(service, task_id, findings=False)
    collection = collect(root, run_id, tmp_path / "collection", "paper", "zero")
    packet = blind([collection], tmp_path / "packet", 13)
    assert read_json(packet / "public/input.json")["candidates"] == []
    path = save(tmp_path / "judge.json", judgment(packet))
    result = summarize(packet, [path], tmp_path / "results")
    assert result["trials"][0]["has_accepted_review"]
    assert result["trials"][0]["candidate_count"] == 0
    assert result["candidates"] == {}


def test_different_manuscript_inputs_remain_visible_in_comparison(tmp_path):
    root = project(tmp_path)
    paths = []
    for number in range(2):
        if number:
            source = root / "main.tex"
            source.write_text(source.read_text() + "% New manuscript revision\n")
            subprocess.run(["git", "-C", str(root), "add", "main.tex"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.test",
                    "commit",
                    "-qm",
                    "Changed manuscript",
                ],
                check=True,
            )
        run_id, task_id = start(root)
        with ScriptoriumService(root) as service:
            submit(service, task_id, session=f"review-{number}")
        paths.append(collect(root, run_id, tmp_path / f"collection-{number}", "paper", f"trial-{number}"))
    packet = blind(paths, tmp_path / "packet", 13)
    result = summarize(packet, [], tmp_path / "results")
    assert {"commit", "tree", "sources"}.issubset(result["input_comparisons"][0]["varying_inputs"])
    assert len(result["candidates"]) == 2

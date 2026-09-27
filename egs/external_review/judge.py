import argparse
from collections import Counter, defaultdict
from pathlib import Path
import random
import shutil
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from scriptorium.domain import digest_json

from .files import publication, read_json, seal, verify_seal, write_json

Text = Annotated[str, Field(min_length=1)]


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: Text
    verdict: Literal["supported_concern", "not_supported", "uncertain"]
    significance: Literal["consequential", "minor", "not_evaluable"]
    evidence_locations: list[Text]
    counterevidence: Text
    rationale: Text
    duplicates: list[Text]


class JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    packet_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    client: Text
    model: Text
    effort: Text
    session_id: Text
    session_source: Literal["host", "declared"]
    cost_usd: Annotated[float, Field(ge=0)] | None
    judgments: list[Judgment]
    limitations: list[Text]


def comparison_fields(collection):
    inputs = collection["inputs"]
    frozen = inputs["frozen_config"]
    bundle = {item["path"]: item["digest"] for item in inputs["bundle"]}
    return {
        "commit": inputs["commit_sha"],
        "tree": inputs["tree_sha"],
        "roles": frozen["profile_roles"],
        "sources": frozen["sources"],
        "navigation": frozen["navigation"]["digest"],
        "prompts": {role: value["digest"] for role, value in frozen["prompts"].items()},
        "schemas": {kind: value["digest"] for kind, value in frozen["schemas"].items()},
        "evidence_contract": frozen["evidence_anchor_contract"]["digest"],
        "pdf": bundle["manuscript.pdf"],
        "pages": {name: digest for name, digest in bundle.items() if name.startswith("pages/")},
    }


def blind(collections, output, seed):
    materials = {}
    candidates = {}
    trials = []
    sessions = set()
    identities = set()
    run_ids = set()
    collection_digests = []
    for directory in collections:
        collection_digest = verify_seal(directory)
        collection_digests.append((directory, collection_digest))
        collection = read_json(directory / "collection.json")
        identity = (collection["case"], collection["trial"])
        if identity in identities:
            raise ValueError(f"Duplicate case/trial: {identity}")
        identities.add(identity)
        if collection["run_id"] in run_ids:
            raise ValueError("The same run cannot count as two trials")
        run_ids.add(collection["run_id"])
        report = read_json(directory / "report.json")
        summary = read_json(directory / "summary.json")
        trials.append(
            {
                "case": identity[0],
                "trial": identity[1],
                "collection_digest": collection_digest,
                "comparison": comparison_fields(collection),
                "workflow": summary,
            }
        )
        material_digest = digest_json(collection["inputs"]["bundle"])
        materials.setdefault(material_digest, directory / "bundle")
        for item in report["tasks"]:
            for attempt in item["attempts"]:
                if attempt["thread_id"]:
                    sessions.add((attempt["external_client"], attempt["thread_id"]))
        accepted = {
            attempt["id"]
            for item in report["tasks"]
            if item["task"]["stage"] == "review"
            for attempt in item["attempts"]
            if attempt["status"] == "completed"
        }
        for entry in report["findings"]:
            finding = entry["finding"]
            if finding["attempt_id"] not in accepted:
                raise ValueError("Finding does not belong to an accepted review attempt")
            payload = {key: finding[key] for key in ("title", "claim", "evidence", "explanation", "suggested_action")}
            key = digest_json({"material": material_digest, "finding": payload})
            candidate = candidates.setdefault(key, {"material": material_digest, "finding": payload, "origins": []})
            candidate["origins"].append(
                {
                    "case": identity[0],
                    "trial": identity[1],
                    "finding_id": finding["id"],
                    "task_id": finding["task_id"],
                    "attempt_id": finding["attempt_id"],
                    "role": finding["role"],
                    "severity": finding["severity"],
                    "confidence": finding["confidence"],
                }
            )
            for event in report["events"]:
                if event["event_type"] == "finding.duplicate" and event["entity_id"] == finding["id"]:
                    candidate["origins"].append(
                        {
                            "case": identity[0],
                            "trial": identity[1],
                            "finding_id": finding["id"],
                            "duplicate_event_id": event["id"],
                            **event["payload"],
                        }
                    )
    order = sorted(candidates)
    random.Random(seed).shuffle(order)
    material_ids = {digest: f"M{number:03d}" for number, digest in enumerate(sorted(materials), 1)}
    public = []
    mapping = {}
    for number, key in enumerate(order, 1):
        candidate = candidates[key]
        candidate_id = f"C{number:04d}"
        public.append(
            {"candidate_id": candidate_id, "material": material_ids[candidate["material"]], **candidate["finding"]}
        )
        mapping[candidate_id] = candidate["origins"]
    with publication(output, collections) as stage:
        workspace = stage / "public"
        workspace.mkdir()
        for digest, source in materials.items():
            shutil.copytree(source, workspace / "material" / material_ids[digest])
        write_json(workspace / "input.json", {"candidates": public})
        write_json(workspace / "schema.json", JudgeOutput.model_json_schema())
        shutil.copyfile(Path(__file__).with_name("judge-prompt.md"), workspace / "prompt.md")
        packet_digest = seal(workspace)
        write_json(
            stage / "mapping.json",
            {
                "packet_digest": packet_digest,
                "seed": seed,
                "candidates": mapping,
                "trials": trials,
                "review_sessions": sorted(sessions),
            },
        )
        for directory, expected_digest in collection_digests:
            if verify_seal(directory) != expected_digest:
                raise ValueError(f"Collection has changed during preparation: {directory}")
    return output


def validate_judge(raw, packet_digest, candidate_ids, review_sessions):
    output = JudgeOutput.model_validate_json(raw)
    if output.packet_digest != packet_digest:
        raise ValueError("Judge output belongs to a different packet")
    identity = (output.client, output.session_id)
    if identity in review_sessions:
        raise ValueError("Judge reuses a recorded review session")
    ids = [item.candidate_id for item in output.judgments]
    if len(ids) != len(set(ids)) or set(ids) != candidate_ids:
        raise ValueError("Judge must assess every candidate exactly once")
    for item in output.judgments:
        if item.candidate_id in item.duplicates or not set(item.duplicates).issubset(candidate_ids):
            raise ValueError("Invalid duplicate candidate reference")
    return output


def summarize(packet, judgment_paths, output):
    packet_seal_digest = verify_seal(packet)
    packet_digest = verify_seal(packet / "public")
    mapping = read_json(packet / "mapping.json")
    candidate_ids = set(mapping["candidates"])
    review_sessions = {tuple(item) for item in mapping["review_sessions"]}
    judgments = []
    raw_outputs = []
    seen_sessions = set()
    for path in judgment_paths:
        raw = path.read_bytes()
        judge = validate_judge(raw, packet_digest, candidate_ids, review_sessions)
        identity = (judge.client, judge.session_id)
        if identity in seen_sessions:
            raise ValueError("Duplicate judge session; do not count one judge twice")
        seen_sessions.add(identity)
        judgments.append(judge)
        raw_outputs.append(raw)
    by_candidate = {candidate_id: [] for candidate_id in sorted(candidate_ids)}
    by_case = defaultdict(list)
    trials = []
    for trial in mapping["trials"]:
        ids = {
            candidate_id
            for candidate_id, origins in mapping["candidates"].items()
            if any((origin["case"], origin["trial"]) == (trial["case"], trial["trial"]) for origin in origins)
        }
        assessments = []
        accepted = trial["workflow"]["attempt_status_counts"].get("completed", 0) > 0
        for number, judge in enumerate(judgments, 1):
            if not accepted:
                continue
            selected = [item for item in judge.judgments if item.candidate_id in ids]
            assessments.append(
                {
                    "judge": number,
                    "verdicts": dict(Counter(item.verdict for item in selected)),
                    "supported_consequential": sum(
                        item.verdict == "supported_concern" and item.significance == "consequential"
                        for item in selected
                    ),
                }
            )
        trials.append(
            {
                "case": trial["case"],
                "trial": trial["trial"],
                "workflow": trial["workflow"],
                "candidate_count": len(ids),
                "has_accepted_review": accepted,
                "scientific_assessments": assessments,
            }
        )
        by_case[trial["case"]].append(trial["comparison"])
    for number, judge in enumerate(judgments, 1):
        for item in judge.judgments:
            by_candidate[item.candidate_id].append({"judge": number, **item.model_dump()})
    comparisons = [
        {
            "case": case,
            "trial_count": len(values),
            "varying_inputs": [
                field for field in values[0] if len({digest_json(value[field]) for value in values}) > 1
            ],
        }
        for case, values in by_case.items()
    ]
    result = {
        "packet_digest": packet_digest,
        "packet_seal_digest": packet_seal_digest,
        "formal_benchmark": False,
        "scientific_status": "model_judgments" if judgments else "not_judged",
        "trials": trials,
        "input_comparisons": comparisons,
        "judges": [
            {key: value for key, value in judge.model_dump().items() if key != "judgments"} for judge in judgments
        ],
        "candidates": by_candidate,
        "disagreements": [
            candidate_id
            for candidate_id, items in by_candidate.items()
            if len({(item["verdict"], item["significance"]) for item in items}) > 1
        ],
        "limits": [
            "Model judgments are not human adjudication or gold labels.",
            "Matching frozen inputs does not equal matching host, effort, cost or independent model families.",
            "Tool returns do not prove image viewing, comprehension or exhaustive review.",
            "Only exact candidate duplicates on identical material were combined; "
            "semantic duplicates remain judge annotations.",
        ],
    }
    with publication(output, [packet]) as stage:
        write_json(stage / "summary.json", result)
        for number, raw in enumerate(raw_outputs, 1):
            (stage / f"judge-{number:03d}.json").write_bytes(raw)
        if verify_seal(packet) != packet_seal_digest:
            raise ValueError("Collection has changed during summarization")
    return result


def main():
    parser = argparse.ArgumentParser(description="Prepare anonymous judgments and collect external judge results.")
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--collection", type=Path, action="append", required=True)
    preparation.add_argument("--seed", type=int, required=True)
    preparation.add_argument("--output", type=Path, required=True)
    summary = commands.add_parser("summarize")
    summary.add_argument("--packet", type=Path, required=True)
    summary.add_argument("--judgment", type=Path, action="append", default=[])
    summary.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            print(blind(args.collection, args.output, args.seed))
        else:
            summarize(args.packet, args.judgment, args.output)
            print(args.output)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"Evaluation failed: {exc}\n")


if __name__ == "__main__":
    main()

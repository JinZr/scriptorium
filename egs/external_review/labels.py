import argparse
from collections import defaultdict
from pathlib import Path
import shutil
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .files import publication, read_json, verify_seal, write_json

Text = Annotated[str, Field(min_length=1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitObject = Annotated[str, Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")]


class Problem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Text
    kind: Literal["planted", "documented"]
    summary: Text
    locations: Annotated[list[Text], Field(min_length=1)]


class LabeledCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    case: Text
    tree_sha: GitObject
    problems: list[Problem] = []
    control_of: Text | None = None
    corrected: list[Text] = []

    @model_validator(mode="after")
    def validate_role(self):
        if self.control_of is None:
            ids = [problem.id for problem in self.problems]
            if not ids or len(ids) != len(set(ids)) or self.corrected:
                raise ValueError(f"Labeled case {self.case} lists unique problems and no corrections")
        elif self.problems or not self.corrected or len(self.corrected) != len(set(self.corrected)):
            raise ValueError(f"Control case {self.case} lists unique corrected problems and no problems")
        return self


class LabelSet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    label_set: Text
    annotators: Annotated[list[Text], Field(min_length=1)]
    cases: Annotated[list[LabeledCase], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_cases(self):
        cases = {item.case: item for item in self.cases}
        if len(cases) != len(self.cases):
            raise ValueError("Each case is labeled once")
        for item in self.cases:
            if item.control_of is None:
                continue
            original = cases.get(item.control_of)
            if original is None or original.control_of is not None:
                raise ValueError(f"Control case {item.case} must correct a labeled case")
            if not set(item.corrected) <= {problem.id for problem in original.problems}:
                raise ValueError(f"Control case {item.case} corrects an unknown problem")
        return self

    def labeled(self, case):
        item = next((value for value in self.cases if value.case == case), None)
        if item is None:
            raise ValueError(f"Case has no human labels: {case}")
        return item

    def allowed(self, case):
        item = self.labeled(case)
        return set(item.corrected) if item.control_of else {problem.id for problem in item.problems}

    def check_trials(self, trials):
        """Each trial covers every labeled case, collected from the manuscript tree its labels describe."""
        by_name = defaultdict(set)
        for trial in trials:
            tree = self.labeled(trial["case"]).tree_sha
            if trial["comparison"]["tree"] != tree:
                raise ValueError(
                    f"Trial {trial['trial']} of {trial['case']} was not collected from labeled tree {tree}"
                )
            by_name[trial["trial"]].add(trial["case"])
        for name, cases in sorted(by_name.items()):
            if missing := sorted({item.case for item in self.cases} - cases):
                raise ValueError(f"Trial {name} is missing labeled cases: {', '.join(missing)}")


class Match(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: Text
    problems: list[Text]


class Matches(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    packet_digest: Digest
    labels_digest: Digest
    matchers: Annotated[list[Text], Field(min_length=1)]
    matches: list[Match]


def seal_labels(path, output):
    raw = path.read_bytes()
    LabelSet.model_validate_json(raw)
    with publication(output) as stage:
        (stage / "labels.json").write_bytes(raw)
    return output


def validate_matches(raw, packet_digest, labels_digest, labels, origins):
    matches = Matches.model_validate_json(raw)
    if matches.packet_digest != packet_digest or matches.labels_digest != labels_digest:
        raise ValueError("Matches belong to a different packet or label set")
    ids = [item.candidate_id for item in matches.matches]
    if len(ids) != len(set(ids)) or set(ids) != set(origins):
        raise ValueError("Matches must annotate every candidate exactly once")
    for item in matches.matches:
        if len(item.problems) != len(set(item.problems)):
            raise ValueError(f"Candidate {item.candidate_id} repeats a problem")
        for case in {origin["case"] for origin in origins[item.candidate_id]}:
            if not set(item.problems) <= labels.allowed(case):
                raise ValueError(f"Candidate {item.candidate_id} matches a problem not labeled for {case}")
    return {item.candidate_id: set(item.problems) for item in matches.matches}


def score_trial(trial, labels, matched, origins):
    case = next(item for item in labels.cases if item.case == trial["case"])
    ids = {
        candidate_id
        for candidate_id, values in origins.items()
        if any((origin["case"], origin["trial"]) == (trial["case"], trial["trial"]) for origin in values)
    }
    reported = set().union(*(matched[candidate_id] for candidate_id in ids))
    result = {
        "case": trial["case"],
        "trial": trial["trial"],
        "host_computation": trial["comparison"].get("host_computation", "unknown"),
        "has_accepted_review": trial["workflow"]["attempt_status_counts"].get("completed", 0) > 0,
        "candidate_count": len(ids),
        "unmatched_candidates": sum(not matched[candidate_id] for candidate_id in ids),
    }
    if case.control_of:
        return {**result, "control_of": case.control_of, "false_alarms": sorted(reported), "corrected": case.corrected}
    return {
        **result,
        "found": sorted(reported),
        "missed": sorted({problem.id for problem in case.problems} - reported),
        "found_by_kind": {
            kind: [sum(problem.id in reported for problem in group), len(group)]
            for kind in ("planted", "documented")
            if (group := [problem for problem in case.problems if problem.kind == kind])
        },
    }


def aggregate(trials):
    by_name = defaultdict(list)
    for trial in trials:
        by_name[trial["trial"]].append(trial)
    rows = []
    for name, items in sorted(by_name.items()):
        labeled = [item for item in items if "found" in item]
        controls = [item for item in items if "false_alarms" in item]
        found = sum(len(item["found"]) for item in labeled)
        total = found + sum(len(item["missed"]) for item in labeled)
        alarms = sum(len(item["false_alarms"]) for item in controls)
        corrected = sum(len(item["corrected"]) for item in controls)
        rows.append(
            {
                "trial": name,
                "cases": len(items),
                "accepted_reviews": sum(item["has_accepted_review"] for item in items),
                "host_computation": sorted({item["host_computation"] for item in items}),
                "labeled_problems_found": [found, total],
                "labeled_recall": found / total if total else None,
                "control_false_alarms": [alarms, corrected],
            }
        )
    return rows


def score(packet, labels_dir, matches_path, output):
    packet_seal_digest = verify_seal(packet)
    packet_digest = verify_seal(packet / "public")
    labels_digest = verify_seal(labels_dir)
    labels = LabelSet.model_validate_json((labels_dir / "labels.json").read_bytes())
    mapping = read_json(packet / "mapping.json")
    origins = mapping["candidates"]
    raw = matches_path.read_bytes()
    matched = validate_matches(raw, packet_digest, labels_digest, labels, origins)
    labels.check_trials(mapping["trials"])
    trials = [score_trial(trial, labels, matched, origins) for trial in mapping["trials"]]
    result = {
        "packet_digest": packet_digest,
        "packet_seal_digest": packet_seal_digest,
        "labels_digest": labels_digest,
        "label_set": labels.label_set,
        "annotators": labels.annotators,
        "matchers": Matches.model_validate_json(raw).matchers,
        "trials": trials,
        "by_trial": aggregate(trials),
        "limits": [
            "Labels and candidate matches are human judgments supplied by the operator; this tool generates neither.",
            "Recall covers only the labeled problems. Unmatched candidates are not scored as false positives.",
            "Control false alarms count candidates matched to corrected problems on the corrected manuscript.",
            "Trials without an accepted review keep their labeled problems in the denominator.",
            "Host computation is the operator's planned condition, not an observation of tool use;"
            " collections without a baseline sealed before review report it as unknown.",
        ],
    }
    with publication(output, [packet, labels_dir]) as stage:
        write_json(stage / "score.json", result)
        (stage / "matches.json").write_bytes(raw)
        shutil.copyfile(labels_dir / "labels.json", stage / "labels.json")
        if verify_seal(packet) != packet_seal_digest or verify_seal(labels_dir) != labels_digest:
            raise ValueError("Packet or labels changed during scoring")
    return result


def main():
    parser = argparse.ArgumentParser(description="Seal human problem labels and score human-matched candidates.")
    commands = parser.add_subparsers(dest="command", required=True)
    sealing = commands.add_parser("seal")
    sealing.add_argument("--labels", type=Path, required=True)
    sealing.add_argument("--output", type=Path, required=True)
    scoring = commands.add_parser("score")
    scoring.add_argument("--packet", type=Path, required=True)
    scoring.add_argument("--labels", type=Path, required=True)
    scoring.add_argument("--matches", type=Path, required=True)
    scoring.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "seal":
            print(seal_labels(args.labels, args.output))
        else:
            score(args.packet, args.labels, args.matches, args.output)
            print(args.output)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"Labels failed: {exc}\n")


if __name__ == "__main__":
    main()

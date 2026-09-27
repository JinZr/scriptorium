import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

import scriptorium
from scriptorium.domain import digest_json
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES

from .files import contained_file, file_records, publication, read_json, verify_seal, write_json


def cli(project, arguments, records):
    result = subprocess.run(
        [sys.executable, "-m", "scriptorium", *arguments], cwd=project, capture_output=True, text=True
    )
    records.append(
        {"arguments": arguments, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    )
    if result.returncode:
        raise ValueError(f"Scriptorium inspection failed: {result.stdout or result.stderr}")
    if len(result.stdout.encode("utf-8")) > MAX_TOOL_RESPONSE_BYTES:
        raise ValueError("Inspection response exceeds the CLI byte bound")
    response = json.loads(result.stdout)
    if not response["ok"]:
        raise ValueError(f"Scriptorium rejected inspection: {response}")
    return response["data"]


def report_parts(project, run_id, records):
    status = cli(project, ["--json", "run", "status", run_id], records)
    report = {}
    report_digest = None
    for part, command in status["report_parts"].items():
        arguments = shlex.split(command)[1:]
        if report_digest is not None:
            arguments += ["--report-digest", report_digest]
        text = ""
        while arguments:
            fragment = cli(project, arguments, records)
            report_digest = report_digest or fragment["report_digest"]
            if (
                fragment["report_digest"] != report_digest
                or fragment["part"] != part
                or fragment["offset"] != len(text)
            ):
                raise ValueError("Report fragments do not describe the same frozen view")
            text += fragment["text"]
            arguments = shlex.split(fragment["next_command"])[1:] if fragment["next_command"] else None
        if len(text) != fragment["total_chars"] or sha256(text.encode()).hexdigest() != fragment["digest"]:
            raise ValueError(f"Incomplete report part: {part}")
        report[part] = json.loads(text)
    if digest_json(report) != report_digest:
        raise ValueError("Reconstructed report digest does not match")
    return report


def artifact(project, digest):
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid artifact digest")
    path = contained_file(project / ".scriptorium/artifacts/sha256", f"{digest[:2]}/{digest[2:]}")
    data = path.read_bytes()
    if sha256(data).hexdigest() != digest:
        raise ValueError(f"Corrupt artifact: {digest}")
    return data


def copy_bundle(project, report, stage):
    run = report["run"]
    run_dir = project / ".scriptorium/runs" / run["id"]
    manifest = read_json(run_dir / "manifest.json")
    if manifest["run_id"] != run["id"]:
        raise ValueError("Run manifest identity changed")
    for key in ("commit_sha", "tree_sha", "config_digest"):
        if manifest[key] != run[key]:
            raise ValueError(f"Run manifest {key} changed")
    index = json.loads(artifact(project, manifest["bundle_digest"]))
    if file_records(run_dir / "bundle") != index:
        raise ValueError("Frozen bundle differs from its artifact index")
    for entry in index:
        source = contained_file(run_dir / "bundle", entry["path"])
        data = source.read_bytes()
        if len(data) != entry["size"] or sha256(data).hexdigest() != entry["digest"]:
            raise ValueError(f"Frozen bundle changed: {entry['path']}")
        target = stage / "bundle" / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return index


def review_summary(report):
    tasks = [item for item in report["tasks"] if item["task"]["stage"] == "review"]
    attempts = [attempt for item in tasks for attempt in item["attempts"]]
    latest_scopes = {item["task_id"]: item["scope"] for item in report["review_scopes"]}
    task_ids = {item["task"]["id"] for item in tasks}
    transitions = [
        event["payload"]
        for event in report["events"]
        if event["event_type"] == "task.status_changed" and event["entity_id"] in task_ids
    ]
    return {
        "run_status": report["run"]["status"],
        "attempt_status_counts": dict(Counter(item["status"] for item in attempts)),
        "attempts": attempts,
        "continuations": sum(item == {"from": "completed", "to": "pending"} for item in transitions),
        "retries": sum(item["from"] in {"failed", "interrupted"} and item["to"] == "pending" for item in transitions),
        "required_reviews_completed": report["gate"]["conditions"]["required_reviews_completed"],
        "tasks": [
            {
                "task_id": item["task"]["id"],
                "role": item["task"]["role"],
                "status": item["task"]["status"],
                "scope": latest_scopes.get(item["task"]["id"]),
            }
            for item in tasks
        ],
        "accepted_findings": len(report["findings"]),
        "tool_returns": report["review_tool_access"],
        "coverage_audit": report["review_coverage_audit"],
        "image_views_verified": None,
        "scientific_correctness": "not_evaluated",
        "cost_usd": report["run"]["estimated_cost_usd"],
    }


def collect(project, run_id, output, case, trial, baseline=None, host_records=()):
    project = project.resolve()
    if not (project / ".scriptorium/state.sqlite3").is_file():
        raise ValueError("Project must have an existing Scriptorium run")
    if output.resolve().is_relative_to((project / ".scriptorium").resolve()):
        raise ValueError("Evaluation output must be outside Scriptorium state")
    records = []
    with publication(output, [baseline] if baseline is not None else []) as stage:
        report = report_parts(project, run_id, records)
        if report["run"]["frozen_config"].get("execution") != "external":
            raise ValueError("This example requires an external-harness run")
        if not report["gate"]["conditions"]["review_artifacts_valid"]:
            raise ValueError("Accepted review artifacts failed core validation")
        bundle = copy_bundle(project, report, stage)
        run = report["run"]
        inputs = {
            "commit_sha": run["commit_sha"],
            "tree_sha": run["tree_sha"],
            "frozen_config": run["frozen_config"],
            "bundle": bundle,
        }
        baseline_digest = None
        if baseline is not None:
            baseline_digest = verify_seal(baseline)
            previous = read_json(baseline / "collection.json")
            if (previous["case"], previous["trial"], previous["run_id"], previous["inputs"]) != (
                case,
                trial,
                run_id,
                inputs,
            ):
                raise ValueError("Prepared trial inputs or identity changed")
            if any(item["attempts"] for item in read_json(baseline / "report.json")["tasks"]):
                raise ValueError("Baseline must be collected before the first attempt")
        for item in report["tasks"]:
            for attempt in item["attempts"]:
                for field in (
                    "prompt_digest",
                    "schema_digest",
                    "bundle_digest",
                    "output_artifact_digest",
                    "validation_report_artifact_digest",
                    "trace_artifact_digest",
                ):
                    digest = attempt[field]
                    if digest is not None:
                        target = stage / "artifacts" / digest
                        target.parent.mkdir(exist_ok=True)
                        target.write_bytes(artifact(project, digest))
        for number, path in enumerate(host_records, 1):
            target = stage / "host-records" / f"{number:03d}.bin"
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(path.read_bytes())
        # Recheck after copying to reject a run that advanced during collection.
        cli(
            project,
            ["--json", "run", "report", run_id, "--part", "run", "--report-digest", digest_json(report)],
            records,
        )
        package_root = Path(scriptorium.__file__).parent
        write_json(
            stage / "collection.json",
            {
                "case": case,
                "trial": trial,
                "run_id": run_id,
                "project": str(project),
                "inputs": inputs,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "baseline_digest": baseline_digest,
                "prepared_before_review": baseline_digest is not None,
                "collector_environment": {
                    "python": sys.version,
                    "scriptorium": version("scriptorium"),
                    "pydantic": version("pydantic"),
                    "pymupdf": version("PyMuPDF"),
                    "core_files": [
                        item for item in file_records(package_root) if item["path"].endswith((".py", ".md"))
                    ],
                },
                "collector_files": {
                    path.name: sha256(path.read_bytes()).hexdigest()
                    for path in sorted(Path(__file__).parent.iterdir())
                    if path.is_file() and path.suffix in {".py", ".md"}
                },
                "provenance_limit": (
                    "Attempt identities are host/caller reports. "
                    "Collection code is not proof of review-time code or billing."
                ),
                "host_records": [
                    {"file": f"host-records/{number:03d}.bin", "provided_path": str(path)}
                    for number, path in enumerate(host_records, 1)
                ],
                "formal_benchmark": False,
            },
        )
        write_json(stage / "report.json", report)
        write_json(stage / "inspection-calls.json", records)
        write_json(stage / "summary.json", review_summary(report))
    return output


def main():
    parser = argparse.ArgumentParser(description="Seal a read-only external review snapshot; never launch a model.")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--trial", required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--host-record", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        print(collect(args.project, args.run, args.output, args.case, args.trial, args.baseline, args.host_record))
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"Collection failed: {exc}\n")


if __name__ == "__main__":
    main()

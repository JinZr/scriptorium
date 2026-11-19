from __future__ import annotations

from collections import Counter
import json
import shlex

REPORT_PARTS = (
    "run",
    "tasks",
    "finding_ids",
    "patch_ids",
    "findings",
    "findings_grouped",
    "decision_stats",
    "plans",
    "patches",
    "events",
    "validation_reports",
    "review_limitations",
    "review_claim_checks",
    "gate",
)


def success_json(payload):
    return json.dumps({"ok": True, "data": payload}, ensure_ascii=False, separators=(",", ":"))


def pretty_json(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2)


def tool_command(*arguments):
    return shlex.join(["scriptorium", "--json", "task", *map(str, arguments)])


def report_command(run_id, part):
    return shlex.join(["scriptorium", "--json", "run", "report", run_id, "--part", part])


def brief_summary(brief):
    return {"digest": brief.digest, **brief.fields()}


def run_overview(view, next_actions=None):
    run = view["run"]
    optional = {}
    if "review_brief" in view:
        optional["review_brief"] = brief_summary(view["review_brief"])
    if "detected_template" in view:
        optional["detected_template"] = view["detected_template"]
    return {
        "run": {"id": run.id, "status": run.status.value, "commit_sha": run.commit_sha},
        **optional,
        "task_counts": dict(Counter(item["task"].status.value for item in view["tasks"])),
        "finding_count": len(view["finding_ids"]),
        "patch_count": len(view["patch_ids"]),
        "has_error": run.error is not None,
        "next_actions": next_actions if next_actions is not None else [{"command": "run status", "run_id": run.id}],
        "report_parts": {part: report_command(run.id, part) for part in REPORT_PARTS},
    }


def run_list(total, rows, limit):
    """Project a newest-first run listing of at most limit rows."""
    entries = []
    for run, task_counts, brief in rows:
        entry = {
            "run_id": run.id,
            "status": run.status.value,
            "commit_sha": run.commit_sha,
            "profile": run.profile,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "task_counts": task_counts,
        }
        if brief is not None:
            entry.update(brief)
        entries.append(entry)
    return {
        "total": total,
        "limit": limit,
        "returned": len(entries),
        "truncated": len(entries) < total,
        "runs": entries,
        "next_actions": [{"command": "run status", "run_id": entry["run_id"]} for entry in entries[:1]],
    }


def task_view(context):
    """Project a claimed attempt into its compact overview; the frozen inputs are read from an export."""
    attempt, task = context["attempt"], context["task"]
    return {
        "run_id": context["run_id"],
        "task": {"id": task.id, "role": task.role.value, "stage": task.stage, "status": task.status.value},
        "attempt": {
            "id": attempt.id,
            "ordinal": attempt.ordinal,
            "status": attempt.status.value,
            "external_client": attempt.external_client,
            "model": attempt.model,
            "effort": attempt.effort,
            "thread_id": attempt.thread_id,
            "session_source": attempt.session_source,
        },
        "input_digest": context["input_digest"],
        "bundle_digest": context["bundle_digest"],
        "navigation_digest": context["navigation_digest"],
        "export_command": tool_command("export", attempt.id, "--dir", "DIR"),
    }

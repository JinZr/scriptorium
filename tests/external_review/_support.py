import asyncio
import json
from pathlib import Path
import subprocess

import fitz

from scriptorium.manuscript import BuildResult, ManuscriptManager
from scriptorium.service import ScriptoriumService


class ManuscriptBuilder(ManuscriptManager):
    def build(self, workspace, manuscript):
        target = workspace / Path(manuscript.main).with_suffix(".pdf")
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "The result is clear.")
            document.save(target, no_new_id=True)
        return BuildResult(pdf_path=target, log="Evaluation fixture build")


def project(tmp_path):
    root = tmp_path / "paper"
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nThe result is teh clear.\n\\end{document}\n"
    )
    (root / "scriptorium.toml").write_text(
        '[manuscript]\nmain="main.tex"\nengine="pdflatex"\n[profiles.trial]\nroles=["copyedit"]\n'
    )
    (root / ".gitignore").write_text(".scriptorium/\n")
    for command in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "Paper"],
    ):
        subprocess.run(["git", "-C", str(root), *command], check=True, capture_output=True)
    return root


def start(root, revision="HEAD"):
    with ScriptoriumService(root, manuscript_manager=ManuscriptBuilder(root)) as service:
        run_id = asyncio.run(service.start_run(revision, "trial", allow_duplicate=True))["run"].id
        task = service.list_tasks(run_id)["tasks"][0]["task"]
    return run_id, task.id


def answer(context, *, completion="complete", findings=True):
    source = next(item for item in context["source_map"]["sources"] if item["source_path"] == "main.tex")
    return {
        "summary": "Reviewed the available manuscript.",
        "findings": (
            [
                {
                    "category": "clarity",
                    "severity": "major",
                    "title": "Typo in result",
                    "claim": "The result sentence has a typo.",
                    "evidence": [
                        {
                            "source_path": "main.tex",
                            "source_digest": source["source_digest"],
                            "start_line": 3,
                            "end_line": 3,
                            "quoted_text": "The result is teh clear.",
                        }
                    ],
                    "explanation": "The word obscures the sentence.",
                    "suggested_action": "Correct teh to the.",
                    "consequence": "A reader would misread the reported result.",
                    "confidence": 0.9,
                }
            ]
            if findings
            else []
        ),
        "scope": {
            "completion": completion,
            "checked": [{"source_path": "main.tex", "start_line": 1, "end_line": 4}],
            "outstanding": [],
            "limitations": ["External data unavailable."] if completion == "partial" else [],
        },
    }


def submit(service, task_id, *, model="model-a", session="review-a", completion="complete", findings=True):
    context = service.claim_task(task_id, "codex", model, "low", session, "host")
    service.read_task(context["attempt"].id, "sources/main.tex", 1, 100, 0, 8000)
    service.page_task(context["attempt"].id, 1)
    receipt = asyncio.run(
        service.submit_task(
            context["attempt"].id,
            context["input_digest"],
            json.dumps(answer(context, completion=completion, findings=findings)),
        )
    )
    assert receipt["attempt"].status.value == "completed"
    return context

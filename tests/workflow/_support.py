from collections import Counter
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess

import fitz

from scriptorium.domain import AgentRole
from scriptorium.manuscript import BuildResult, ManuscriptManager
from scriptorium.runtime import AgentResult, AgentUsage

MANUSCRIPT = "\\documentclass{article}\n" "\\begin{document}\n" "The result is teh clear.\n" "\\end{document}\n"


class PdfBuildingManuscriptManager(ManuscriptManager):
    def build(self, workspace, manuscript):
        pdf_path = workspace / Path(manuscript.main).with_suffix(".pdf")
        pdf_path.unlink(missing_ok=True)
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 72), "The result is clear.")
            document.save(pdf_path)
        finally:
            document.close()
        return BuildResult(pdf_path=pdf_path, log="fake LaTeX build succeeded")


class FakeAgentRuntime:
    def __init__(
        self,
        *,
        interrupt_copyedit_once=False,
        invalid_substantive_once=False,
        runtime_name="codex",
        runtime_version="0.144.4",
        model="fake-model",
        provider="ollama",
    ):
        self.interrupt_copyedit_once = interrupt_copyedit_once
        self.invalid_substantive_once = invalid_substantive_once
        self.runtime_name = runtime_name
        self.runtime_version = runtime_version
        self.model = model
        self.provider = provider
        self.run_calls = Counter()
        self.resume_calls = []
        self.tasks = {}
        self.workspaces = {}
        self.session_dirs = {}
        self.session_dir_calls = []

    async def run_agent(self, task, role, workspace, schema, session_dir, on_session_started=None):
        self.run_calls[role] += 1
        self.tasks[role] = task
        self.workspaces[role] = workspace
        self.session_dirs[role] = session_dir
        self.session_dir_calls.append((role, session_dir))
        ordinal = self.run_calls[role]
        if on_session_started is not None:
            on_session_started(f"thread-{role.value}-{ordinal}")
        if role == AgentRole.COPYEDIT and self.interrupt_copyedit_once and ordinal == 1:
            return self._result(role, ordinal, "interrupted", None)
        if role in {AgentRole.SUBSTANTIVE_REVIEW, AgentRole.COPYEDIT}:
            output = self._review_output(role, workspace)
            if role == AgentRole.SUBSTANTIVE_REVIEW and self.invalid_substantive_once and ordinal == 1:
                output["findings"][0]["evidence"][0]["source_digest"] = "0" * 64
        elif role == AgentRole.REVISION:
            output = self._revision_output(task, workspace)
        elif role == AgentRole.VERIFICATION:
            output = self._verification_output(task)
        else:
            raise AssertionError(f"unexpected role: {role}")
        return self._result(role, ordinal, "completed", output)

    async def resume_agent(
        self,
        thread_id,
        task,
        role,
        workspace,
        schema,
        session_dir,
        on_session_started=None,
    ):
        self.resume_calls.append(role)
        self.tasks[role] = task
        assert session_dir == self.session_dirs[role]
        self.session_dir_calls.append((role, session_dir))
        if on_session_started is not None:
            on_session_started(thread_id)
        if role == AgentRole.COPYEDIT:
            output = {"summary": "No copyediting findings.", "findings": []}
        elif role == AgentRole.SUBSTANTIVE_REVIEW:
            output = self._review_output(role, workspace)
        elif role == AgentRole.REVISION:
            output = self._revision_output(task, workspace)
        else:
            raise AssertionError(f"unexpected resumed role: {role}")
        return replace(self._result(role, 2, "completed", output), thread_id=thread_id)

    @staticmethod
    def _review_output(role, workspace):
        if role == AgentRole.COPYEDIT:
            return {"summary": "No copyediting findings.", "findings": []}
        source = workspace / "sources" / "main.tex"
        return {
            "summary": "One substantive finding.",
            "findings": [
                {
                    "category": "clarity",
                    "severity": "major",
                    "title": "Typo obscures the claim",
                    "claim": "The main result sentence contains a typo.",
                    "evidence": [
                        {
                            "source_path": "main.tex",
                            "start_line": 3,
                            "end_line": 3,
                            "source_digest": sha256(source.read_bytes()).hexdigest(),
                            "quoted_text": "The result is teh clear.",
                        }
                    ],
                    "explanation": "The typo makes the result sentence harder to read.",
                    "suggested_action": "Replace the sentence with the corrected wording.",
                    "confidence": 0.99,
                }
            ],
        }

    @staticmethod
    def _revision_output(task, workspace):
        finding_ids = re.findall(r'"id"\s*:\s*"(finding_[^"]+)"', task)
        source = workspace / "sources" / "main.tex"
        replacement = (
            "The result is clear and precise."
            if "Human rejection feedback:\nnull" not in task
            else "The result is clear."
        )
        return {
            "summary": "Correct the result sentence.",
            "edits": [
                {
                    "finding_ids": finding_ids,
                    "path": "main.tex",
                    "source_digest": sha256(source.read_bytes()).hexdigest(),
                    "start_line": 3,
                    "end_line": 3,
                    "before": "The result is teh clear.",
                    "after": replacement,
                    "rationale": "This directly resolves the confirmed clarity finding.",
                }
            ],
        }

    @staticmethod
    def _verification_output(task):
        finding_ids = re.findall(r'"id"\s*:\s*"(finding_[^"]+)"', task)
        return {
            "verdict": "pass",
            "summary": "The approved edit resolves the finding without regression.",
            "resolved_finding_ids": finding_ids,
            "issues": [],
        }

    def _result(self, role, ordinal, status, output):
        return AgentResult(
            thread_id=f"thread-{role.value}-{ordinal}",
            status=status,
            final_response=json.dumps(output) if output is not None else None,
            usage=AgentUsage(input_tokens=100, output_tokens=20),
            trace_jsonl=json.dumps({"role": role.value, "status": status}) + "\n",
            runtime_name=self.runtime_name,
            runtime_version=self.runtime_version,
            model=self.model,
            model_provider=self.provider,
            duration_ms=5,
            error=None if status == "completed" else "simulated interruption",
        )


def make_repository(tmp_path):
    repo = tmp_path / "paper"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Scriptorium Test")
    _git(repo, "config", "user.email", "scriptorium@example.test")
    (repo / "main.tex").write_text(MANUSCRIPT, encoding="utf-8")
    (repo / "scriptorium.toml").write_text(
        (
            "[manuscript]\n"
            'main = "main.tex"\n'
            'engine = "pdflatex"\n\n'
            "[profiles.quick]\n"
            'roles = ["substantive_review", "copyedit"]\n'
        ),
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(".scriptorium/\n", encoding="utf-8")
    _git(repo, "add", "main.tex", "scriptorium.toml", ".gitignore")
    _git(repo, "commit", "-q", "-m", "Initial manuscript")
    state = repo / ".scriptorium"
    state.mkdir()
    (state / "config.toml").write_text(
        (
            "max_concurrency = 2\n\n"
            "[roles]\n"
            'substantive_review = "primary"\n'
            'copyedit = "primary"\n'
            'visual_transcription = "primary"\n'
            'revision = "primary"\n'
            'verification = "primary"\n\n'
            "[routes.primary]\n"
            'model_provider = "ollama"\n'
            'model = "fake-model"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
            'reasoning_effort = "high"\n'
        ),
        encoding="utf-8",
    )
    return repo


def _git(repo, *arguments):
    subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True)

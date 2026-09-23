import asyncio
from dataclasses import replace
from hashlib import sha256
import json

import pytest

from scriptorium.domain import AgentRole, RunStatus
from scriptorium.errors import InfrastructureError
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

from ._support import FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


def test_preparation_resume_uses_navigation_frozen_before_run_creation(tmp_path, monkeypatch):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime()
    manager = PdfBuildingManuscriptManager(repo)
    with ScriptoriumService(repo, runtime_factory=lambda route: runtime, manuscript_manager=manager) as service:
        original_build = manager.build
        monkeypatch.setattr(manager, "build", lambda *args: (_ for _ in ()).throw(InfrastructureError("build failed")))
        with pytest.raises(InfrastructureError, match="build failed"):
            asyncio.run(service.start_run("HEAD", "quick", None))
        run = service.database.list_runs()[0]
        frozen = run.frozen_config["navigation"]
        assert sha256(frozen["content"].encode()).hexdigest() == frozen["digest"]
        monkeypatch.setattr(manager, "build", original_build)
        monkeypatch.setattr(manager, "create_navigation", lambda *args: pytest.fail("must not regenerate frozen index"))
        monkeypatch.setattr(Armarius, "_prompt_record", lambda *args: pytest.fail("must not regenerate frozen prompts"))
        resumed = asyncio.run(service.resume_run(run.id))["run"]
        assert resumed.config_digest == run.config_digest
        bundle = repo / ".scriptorium/runs" / run.id / "bundle"
        assert (bundle / "navigation.json").read_text() == frozen["content"]
        assert "navigation.json" in runtime.tasks[AgentRole.COPYEDIT]
        assert service.armarius._bundle_for_run(resumed).workspace == bundle


@pytest.mark.parametrize("tamper", ["missing", "changed", "symlink", "manifest", "rewritten"])
def test_base_navigation_damage_blocks_resume_before_provider(tmp_path, tamper):
    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime(interrupt_copyedit_once=True)
    with ScriptoriumService(
        repo, runtime_factory=lambda route: runtime, manuscript_manager=PdfBuildingManuscriptManager(repo)
    ) as service:
        run = asyncio.run(service.start_run("HEAD", "quick", None))["run"]
        bundle = repo / ".scriptorium/runs" / run.id / "bundle"
        path = bundle / "navigation.json"
        if tamper == "missing":
            path.unlink()
        elif tamper == "changed":
            path.write_text("changed")
        elif tamper == "symlink":
            path.rename(bundle / "saved.json")
            path.symlink_to(bundle / "saved.json")
        else:
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            if tamper == "manifest":
                del manifest["navigation_digest"]
            else:
                path.write_text(path.read_text() + "\n")
                manifest["navigation_digest"] = sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(InfrastructureError):
            asyncio.run(service.resume_run(run.id))
        assert runtime.resume_calls == []


def test_historical_run_keeps_bundle_prompt_and_task_identity_without_navigation(tmp_path, monkeypatch):
    original_freeze = Armarius._freeze_config

    def historical_freeze(self, *args):
        frozen = original_freeze(self, *args)
        del frozen["navigation"]
        for role, template in frozen["evidence_anchor_contract"]["prompt_templates"].items():
            frozen["evidence_anchor_contract"]["prompt_templates"][role] = self._content_record(
                "Historical role prompt"
            )
        return frozen

    repo = make_repository(tmp_path)
    runtime = FakeAgentRuntime(interrupt_copyedit_once=True)
    manager = PdfBuildingManuscriptManager(repo)
    monkeypatch.setattr(Armarius, "_freeze_config", historical_freeze)
    with ScriptoriumService(repo, runtime_factory=lambda route: runtime, manuscript_manager=manager) as service:
        run = asyncio.run(service.start_run("HEAD", "quick", None))["run"]
        before = next(task for task in service.database.list_tasks(run.id) if task.role == AgentRole.COPYEDIT)
        bundle = repo / ".scriptorium/runs" / run.id / "bundle"
        before_digest = service.armarius._directory_digest(bundle)
        monkeypatch.setattr(Armarius, "_freeze_config", original_freeze)
        monkeypatch.setattr(manager, "create_navigation", lambda *args: pytest.fail("historical index backfill"))
        asyncio.run(service.resume_run(run.id))
        after = service.database.get_task(before.id)
        assert after.input_digest == before.input_digest
        assert service.armarius._directory_digest(bundle) == before_digest
        assert not (bundle / "navigation.json").exists()
        assert runtime.tasks[AgentRole.COPYEDIT] == "Historical role prompt"
        assert runtime.resume_calls == [AgentRole.COPYEDIT]


class InterruptedVerifier(FakeAgentRuntime):
    async def run_agent(self, task, role, workspace, schema, session_dir, on_session_started=None):
        result = await super().run_agent(task, role, workspace, schema, session_dir, on_session_started)
        if role == AgentRole.VERIFICATION:
            return replace(result, status="interrupted", final_response=None)
        return result

    async def resume_agent(self, thread_id, task, role, workspace, schema, session_dir, on_session_started=None):
        self.resume_calls.append(role)
        return replace(self._result(role, 2, "completed", self._verification_output(task)), thread_id=thread_id)


@pytest.mark.parametrize("tamper", [None, "missing", "stale_sources"])
def test_patched_navigation_binds_new_sources_and_is_not_repaired_on_resume(tmp_path, tamper, monkeypatch):
    repo = make_repository(tmp_path)
    runtime = InterruptedVerifier()
    manager = PdfBuildingManuscriptManager(repo)
    with ScriptoriumService(repo, runtime_factory=lambda route: runtime, manuscript_manager=manager) as service:
        run = asyncio.run(service.start_run("HEAD", "quick", None))["run"]
        service.decide_finding(service.list_findings(run.id)[0].id, "confirm", "Correct the typo")
        revised = asyncio.run(service.resume_run(run.id))
        patch_id = revised["patch_ids"][0]
        service.decide_patch(patch_id, "approve", "Approved")
        verified = asyncio.run(service.resume_run(run.id))
        assert verified["run"].status == RunStatus.VERIFYING
        bundle = repo / ".scriptorium/runs" / run.id / "verifications" / patch_id / "bundle"
        path = bundle / "navigation.json"
        navigation = json.loads(path.read_text())
        assert navigation["sources"] != json.loads(run.frozen_config["navigation"]["content"])["sources"]
        assert navigation["sources"][0]["digest"] == sha256((bundle / "sources/main.tex").read_bytes()).hexdigest()
        if tamper == "missing":
            path.unlink()
        elif tamper == "stale_sources":
            path.write_text(run.frozen_config["navigation"]["content"])
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["navigation_digest"] = sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
        monkeypatch.setattr(manager, "create_navigation", lambda *args: pytest.fail("completed index regenerated"))
        if tamper is None:
            resumed = asyncio.run(service.resume_run(run.id))
            assert resumed["run"].status == RunStatus.READY_TO_APPLY
            assert runtime.resume_calls == [AgentRole.VERIFICATION]
        else:
            with pytest.raises(InfrastructureError):
                asyncio.run(service.resume_run(run.id))
            assert AgentRole.VERIFICATION not in runtime.resume_calls
        assert runtime.run_calls[AgentRole.VERIFICATION] == 1

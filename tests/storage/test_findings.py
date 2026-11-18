from scriptorium.domain import AgentRole, Finding, FindingSeverity, FindingStatus
from scriptorium.storage import Database

from ._factories import create_completed_attempt, make_run


def test_finding_decision_is_atomic_and_history_is_immutable(tmp_path) -> None:
    with Database(tmp_path) as database:
        run = database.create_run(make_run())
        task, attempt = create_completed_attempt(database, run)
        finding = database.create_finding(
            Finding(
                run_id=run.id,
                task_id=task.id,
                attempt_id=attempt.id,
                fingerprint="f" * 64,
                role=AgentRole.COPYEDIT,
                category="language",
                severity=FindingSeverity.MAJOR,
                title="Ambiguous wording",
                claim="The sentence is ambiguous.",
                evidence=({"source_path": "main.tex", "start_line": 10, "end_line": 10},),
                explanation="Two readings are possible.",
                suggested_action="Rewrite the sentence.",
                confidence=0.9,
                consequence="A reader cannot tell which reading is meant.",
            )
        )

        decision = database.decide_finding(finding.id, "confirm", "The ambiguity is real.")

        assert database.get_finding(finding.id).status == FindingStatus.CONFIRMED
        assert database.list_decisions("finding", finding.id) == [decision]
        later = database.decide_finding(finding.id, "waive", "Out of scope for this draft.")

        assert database.get_finding(finding.id).status == FindingStatus.WAIVED
        assert database.list_decisions("finding", finding.id) == [decision, later]
        assert [event.event_type for event in database.list_events(run.id)].count("finding.decided") == 2


def test_findings_deduplicate_only_by_exact_fingerprint(tmp_path) -> None:
    with Database(tmp_path) as database:
        run = database.create_run(make_run())
        task, attempt = create_completed_attempt(database, run)

        def finding(fingerprint: str, title: str) -> Finding:
            return Finding(
                run_id=run.id,
                task_id=task.id,
                attempt_id=attempt.id,
                fingerprint=fingerprint,
                role=AgentRole.COPYEDIT,
                category="language",
                severity=FindingSeverity.MODERATE,
                title=title,
                claim="The sentence is ambiguous.",
                evidence=({"source_path": "main.tex", "start_line": 10, "end_line": 10},),
                explanation="Two readings are possible.",
                suggested_action="Rewrite the sentence.",
                confidence=0.9,
                consequence="A reader cannot tell which reading is meant.",
            )

        first = database.get_or_create_finding(finding("a" * 64, "Ambiguous wording"))
        duplicate = database.get_or_create_finding(finding("a" * 64, "Ambiguous wording"))
        similar = database.get_or_create_finding(finding("b" * 64, "Slightly ambiguous wording"))

        assert duplicate.id == first.id
        assert similar.id != first.id
        assert len(database.list_findings(run.id)) == 2

"""The fact-change list of a candidate patch: what its navigation index and page counts lost and gained.

A pure, deterministic comparison of the run's frozen navigation index with the patched bundle's, and of the two
bundles' document indexes. The navigation index is heuristic, so the list is a reading aid for the lead and the
author: it is not evidence, an empty list does not show that no fact changed, and nothing is gated on it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from .manuscript import QUANTITY_COMMANDS

CITATION_COMMANDS = ("cite", "citep", "citet", "autocite", "parencite", "textcite")
REFERENCE_COMMANDS = ("ref", "eqref", "pageref", "autoref", "cref", "Cref")
FACT_COMMANDS = frozenset(
    {"quantity", *QUANTITY_COMMANDS, *CITATION_COMMANDS, *REFERENCE_COMMANDS, "label", "includegraphics"}
)
FACT_LIST_LIMIT = 100
FACT_LIST_NOTE = (
    "A reading aid compared from the heuristic navigation index, not evidence. An empty list does not show that no "
    "fact changed, and nothing is gated on it."
)


def fact_changes(
    before_entries: Iterable[Mapping[str, Any]],
    after_entries: Iterable[Mapping[str, Any]],
    before_documents: Sequence[Mapping[str, Any]],
    after_documents: Sequence[Mapping[str, Any]],
    *,
    limit: int = FACT_LIST_LIMIT,
) -> dict[str, Any]:
    """Compare two navigation indexes as multisets of ``(command, value)`` and two document indexes by entrypoint.

    A removed entry carries its location before the patch, an added one its location after. When a value occurs
    more often on one side, the earliest surplus occurrences in source order are listed. Each list keeps at most
    ``limit`` entries and counts what it cut.
    """
    before = _facts(before_entries)
    after = _facts(after_entries)
    removed = _surplus(before, after)
    added = _surplus(after, before)
    return {
        "note": FACT_LIST_NOTE,
        "commands": sorted(FACT_COMMANDS),
        "removed": removed[:limit],
        "removed_omitted": max(0, len(removed) - limit),
        "added": added[:limit],
        "added_omitted": max(0, len(added) - limit),
        "pages": _pages(before_documents, after_documents),
    }


def _facts(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    facts = [
        {
            "command": entry["command"],
            "value": entry["value"],
            "source_path": entry["source_path"],
            "start_line": entry["start_line"],
        }
        for entry in entries
        if entry["command"] in FACT_COMMANDS
    ]
    return sorted(facts, key=lambda fact: (fact["source_path"], fact["start_line"], fact["command"], fact["value"]))


def _surplus(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remaining = Counter((fact["command"], fact["value"]) for fact in first)
    remaining.subtract(Counter((fact["command"], fact["value"]) for fact in second))
    surplus = []
    for fact in first:
        key = (fact["command"], fact["value"])
        if remaining[key] > 0:
            surplus.append(fact)
            remaining[key] -= 1
    return surplus


def _pages(before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    for side, documents in (("before", before), ("after", after)):
        for document in documents:
            entry = counts.setdefault(document["entrypoint"], {"document": document["entrypoint"]})
            entry[side] = document["page_count"]
    return [
        {"document": name, "before": item.get("before"), "after": item.get("after")} for name, item in counts.items()
    ]

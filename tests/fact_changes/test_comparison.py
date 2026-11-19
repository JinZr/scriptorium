from scriptorium.fact_changes import FACT_COMMANDS, FACT_LIST_NOTE, fact_changes


def _entry(command, value, line, path="main.tex"):
    return {"command": command, "value": value, "source_path": path, "start_line": line, "end_line": line}


DOCUMENTS = [{"entrypoint": "main.tex", "start_page": 1, "page_count": 4}]
BEFORE = [
    _entry("section", "Results", 3),
    _entry("quantity", "0.82", 5),
    _entry("cite", "smith2020", 6),
    _entry("label", "tab:main", 7),
    _entry("ref", "tab:main", 8),
    _entry("includegraphics", "figures/old.pdf", 9),
    _entry("caption", "Accuracy by benchmark.", 10),
]


def test_an_unchanged_index_lists_nothing_but_the_page_counts():
    facts = fact_changes(BEFORE, BEFORE, DOCUMENTS, DOCUMENTS)
    assert facts == {
        "note": FACT_LIST_NOTE,
        "commands": sorted(FACT_COMMANDS),
        "removed": [],
        "removed_omitted": 0,
        "added": [],
        "added_omitted": 0,
        "pages": [{"document": "main.tex", "before": 4, "after": 4}],
    }
    assert "not evidence" in FACT_LIST_NOTE and "empty list does not show" in FACT_LIST_NOTE


def test_changed_facts_are_listed_with_their_locations_before_and_after():
    after = [
        _entry("section", "Findings", 3),
        _entry("quantity", "0.79", 5),
        _entry("cite", "jones2021", 6),
        _entry("cite", "lee2022", 6),
        _entry("label", "tab:results", 8),
        _entry("ref", "tab:results", 9),
        _entry("includegraphics", "figures/new.pdf", 10),
        _entry("caption", "Accuracy on benchmark one.", 11),
    ]
    supplement = [*DOCUMENTS, {"entrypoint": "supplement.tex", "start_page": 5, "page_count": 2}]
    grown = [{**DOCUMENTS[0], "page_count": 5}, {"entrypoint": "supplement.tex", "start_page": 6, "page_count": 2}]
    facts = fact_changes(BEFORE, after, supplement, grown)
    # Headings and captions are not facts; the changed number, citations, label, references, and graphic are.
    assert [(item["command"], item["value"], item["start_line"]) for item in facts["removed"]] == [
        ("quantity", "0.82", 5),
        ("cite", "smith2020", 6),
        ("label", "tab:main", 7),
        ("ref", "tab:main", 8),
        ("includegraphics", "figures/old.pdf", 9),
    ]
    assert [(item["command"], item["value"], item["start_line"]) for item in facts["added"]] == [
        ("quantity", "0.79", 5),
        ("cite", "jones2021", 6),
        ("cite", "lee2022", 6),
        ("label", "tab:results", 8),
        ("ref", "tab:results", 9),
        ("includegraphics", "figures/new.pdf", 10),
    ]
    assert facts["removed"][0] == {"command": "quantity", "value": "0.82", "source_path": "main.tex", "start_line": 5}
    assert facts["pages"] == [
        {"document": "main.tex", "before": 4, "after": 5},
        {"document": "supplement.tex", "before": 2, "after": 2},
    ]


def test_values_compare_as_multisets_and_moved_facts_are_not_listed():
    before = [_entry("cite", "smith2020", 3), _entry("cite", "smith2020", 9), _entry("SI", "{3}{m}", 4)]
    after = [_entry("cite", "smith2020", 12), _entry("SI", "{3}{m}", 2, "appendix.tex")]
    facts = fact_changes(before, after, [], [{"entrypoint": "main.tex", "start_page": 1, "page_count": 1}])
    # One of two occurrences went; the earliest surplus occurrence is named.
    assert facts["removed"] == [{"command": "cite", "value": "smith2020", "source_path": "main.tex", "start_line": 3}]
    assert facts["added"] == []
    assert facts["pages"] == [{"document": "main.tex", "before": None, "after": 1}]


def test_each_list_is_capped_with_a_count_of_what_was_cut():
    before = [_entry("ref", f"eq:{index}", index) for index in range(1, 8)]
    after = [_entry("ref", f"eq:new{index}", index) for index in range(1, 4)]
    facts = fact_changes(before, after, DOCUMENTS, DOCUMENTS, limit=2)
    assert [item["value"] for item in facts["removed"]] == ["eq:1", "eq:2"] and facts["removed_omitted"] == 5
    assert [item["value"] for item in facts["added"]] == ["eq:new1", "eq:new2"] and facts["added_omitted"] == 1
    # The comparison is deterministic: input order does not matter.
    assert fact_changes(list(reversed(before)), after, DOCUMENTS, DOCUMENTS, limit=2) == facts

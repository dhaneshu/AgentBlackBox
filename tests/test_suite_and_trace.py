"""Suite validation and trace round-tripping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blackbox.corpus import Corpus
from blackbox.suite import Case, load_suite, summarise, validate_suite
from blackbox.trace import Citation, Step, Trace, Violation, load_traces, save_traces


def suite_file(tmp_path, *records) -> Path:
    path = tmp_path / "suite.jsonl"
    path.write_text(
        "# a comment\n\n" + "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def answerable(**overrides) -> dict:
    record = {
        "id": "Q-1",
        "question": "When does production go live?",
        "expect": "answer",
        "must_contain": ["15 October"],
    }
    record.update(overrides)
    return record


def errors_for(tmp_path, *records, corpus=None) -> list[str]:
    return validate_suite(load_suite(suite_file(tmp_path, *records)), corpus).errors


# --- suite -----------------------------------------------------------------

def test_a_well_formed_suite_validates(tmp_path):
    assert errors_for(tmp_path, answerable()) == []


def test_comments_and_blank_lines_are_ignored(tmp_path):
    assert len(load_suite(suite_file(tmp_path, answerable()))) == 1


def test_a_duplicate_id_is_an_error(tmp_path):
    assert any("duplicate id" in e for e in errors_for(tmp_path, answerable(), answerable()))


def test_an_unknown_expectation_is_an_error(tmp_path):
    assert any("is not one of" in e for e in errors_for(tmp_path, answerable(expect="maybe")))


def test_a_refusal_case_cannot_also_demand_content(tmp_path):
    record = answerable(expect="refuse")
    assert any("cannot also require" in e for e in errors_for(tmp_path, record))


def test_an_answer_case_must_assert_something(tmp_path):
    record = answerable(must_contain=[])
    errors = errors_for(tmp_path, record)
    assert any("must assert something" in e for e in errors)


def test_a_refusal_case_needs_no_assertions(tmp_path):
    record = {"id": "Q-9", "question": "What is the penalty?", "expect": "refuse"}
    assert errors_for(tmp_path, record) == []


def test_citing_a_source_outside_the_corpus_is_an_error(tmp_path):
    (tmp_path / "plan.md").write_text("# Plan\n\nGo-live is 15 October.\n", encoding="utf-8")
    corpus = Corpus.load(tmp_path)
    record = answerable(must_cite=["absent.md"])
    assert any("not in the corpus" in e for e in errors_for(tmp_path, record, corpus=corpus))


def test_citing_a_real_source_validates(tmp_path):
    (tmp_path / "plan.md").write_text("# Plan\n\nGo-live is 15 October.\n", encoding="utf-8")
    corpus = Corpus.load(tmp_path)
    record = answerable(must_cite=["plan.md"])
    assert errors_for(tmp_path, record, corpus=corpus) == []


def test_malformed_json_names_the_line(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "Q-1"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_suite(path)


def test_a_missing_suite_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_suite(tmp_path / "absent.jsonl")


def test_summary_separates_answerable_from_refusable():
    cases = [
        Case(id="Q-1", question="a", expect="answer", must_contain=("x",)),
        Case(id="Q-2", question="b", expect="refuse"),
    ]
    assert "1 answerable, 1 that must be refused" in summarise(cases)


# --- trace -----------------------------------------------------------------

def full_trace() -> Trace:
    trace = Trace(
        run_id="r-1", case_id="Q-1", agent="grounded-v1",
        question="When does production go live?",
        answer="Production go-live is 15 October 2026.",
        config={"evidence_floor": 0.35},
        tokens_in=12, tokens_out=9,
    )
    trace.steps.append(Step(0, "receive", "question", "asked"))
    trace.steps.append(
        Step(1, "retrieve", "corpus.search", "1 hit", {"hits": [{"citation": "p.md:1", "score": 0.9}]})
    )
    trace.citations.append(
        Citation("p.md", (11, 11), "Production go-live is 15 October 2026.",
                 "Production go-live is 15 October 2026.", 1.0)
    )
    trace.violations.append(Violation("PII_IN_OUTPUT", "high", "example", 1))
    return trace


def test_a_trace_survives_a_round_trip(tmp_path):
    original = full_trace()
    path = save_traces([original], tmp_path / "run.jsonl")
    restored = load_traces(path)[0]

    assert restored.to_dict() == original.to_dict()
    assert restored.citations[0].lines == (11, 11)
    assert restored.violations[0].policy == "PII_IN_OUTPUT"
    assert restored.steps[1].detail["hits"][0]["score"] == 0.9


def test_config_hash_is_stable_and_sensitive():
    a, b = full_trace(), full_trace()
    assert a.config_hash == b.config_hash
    b.config["evidence_floor"] = 0.65
    assert a.config_hash != b.config_hash


def test_grounded_requires_every_citation_to_hold_up():
    trace = full_trace()
    assert trace.grounded is True
    trace.citations.append(Citation("p.md", (1, 1), "x", "invented claim", 0.0))
    assert trace.grounded is False
    assert len(trace.unsupported_claims) == 1


def test_an_answer_with_no_citations_is_not_grounded():
    trace = full_trace()
    trace.citations = []
    assert trace.grounded is False


def test_total_tokens_add_up():
    assert full_trace().total_tokens == 21


def test_worst_violation_reports_the_highest_severity():
    trace = full_trace()
    trace.violations.append(Violation("UNCITED_ANSWER", "critical", "x"))
    assert trace.worst_violation() == "critical"


def test_a_missing_trace_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_traces(tmp_path / "absent.jsonl")

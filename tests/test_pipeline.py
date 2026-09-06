"""End-to-end: the shipped sample must produce the numbers the README quotes.

If someone changes an agent threshold, the corpus or the suite, these fail —
which is the entire point of shipping an evaluation harness with the project.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blackbox.cli import main
from blackbox.corpus import Corpus
from blackbox.policy import PolicySet
from blackbox.replay import determinism_check, replay, run_suite
from blackbox.score import compare, score_run
from blackbox.suite import load_suite, validate_suite
from blackbox.trace import load_traces

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "samples" / "corpus"
SUITE = ROOT / "samples" / "suites" / "northwind.jsonl"


@pytest.fixture(scope="module")
def loaded():
    return Corpus.load(CORPUS), load_suite(SUITE)


@pytest.fixture(scope="module")
def runs(loaded):
    corpus, cases = loaded
    policies = PolicySet()
    v1 = run_suite("grounded-v1", cases, corpus, policies, run_id="test-v1")
    v2 = replay(v1, "grounded-v2", cases, corpus, policies)
    return v1, v2, score_run(v1, cases), score_run(v2, cases)


def test_the_shipped_suite_validates(loaded):
    corpus, cases = loaded
    report = validate_suite(cases, corpus)
    assert report.ok, report.errors


def test_the_shipped_agent_hallucinates_on_the_cases_it_should(runs):
    _v1, _v2, before, _after = runs
    assert before.hallucination_rate > 0
    hallucinated = {r.case_id for r in before.results if r.verdict == "hallucinated"}
    assert hallucinated == {"Q-004", "Q-005"}


def test_each_hallucination_is_caught_by_a_critical_policy(runs):
    v1, _v2, _before, _after = runs
    for case_id in ("Q-004", "Q-005"):
        trace = next(t for t in v1.traces if t.case_id == case_id)
        assert any(v.severity == "critical" for v in trace.violations), case_id


def test_an_answer_with_no_retrieval_at_all_is_flagged_as_uncited(runs):
    v1, _v2, _before, _after = runs
    trace = next(t for t in v1.traces if t.case_id == "Q-005")
    assert "UNCITED_ANSWER" in {v.policy for v in trace.violations}


def test_the_fixed_agent_eliminates_hallucination(runs):
    _v1, _v2, _before, after = runs
    assert after.hallucination_rate == 0.0
    assert after.groundedness == 1.0
    assert after.violation_count == 0


def test_the_fix_costs_one_over_refusal_and_that_is_reported(runs):
    _v1, _v2, _before, after = runs
    over_refused = {r.case_id for r in after.results if r.verdict == "over_refused"}
    assert over_refused == {"Q-008"}
    assert after.over_refusal_rate > 0


def test_the_comparison_reports_both_the_fixes_and_the_regression(runs):
    _v1, _v2, before, after = runs
    result = compare(before, after)
    assert [c.case_id for c in result.fixed] == ["Q-004", "Q-005"]
    assert [c.case_id for c in result.regressed] == ["Q-008"]
    assert result.net == 1


def test_accuracy_improves_overall(runs):
    _v1, _v2, before, after = runs
    assert before.accuracy == 0.75
    assert after.accuracy == 0.875


def test_the_fix_costs_fewer_tokens_not_more(runs):
    _v1, _v2, before, after = runs
    assert after.total_tokens <= before.total_tokens


def test_both_agents_are_perfectly_repeatable(loaded):
    corpus, cases = loaded
    for agent in ("grounded-v1", "grounded-v2"):
        assert determinism_check(agent, cases, corpus, PolicySet()) == 1.0


def test_every_citation_resolves_to_lines_that_exist(runs):
    v1, v2, _before, _after = runs
    for run in (v1, v2):
        for trace in run.traces:
            for citation in trace.citations:
                path = Path(citation.source)
                assert path.exists(), citation.source
                total = len(path.read_text(encoding="utf-8").splitlines())
                assert 1 <= citation.lines[0] <= citation.lines[1] <= total


def test_every_trace_records_the_configuration_that_produced_it(runs):
    v1, v2, _before, _after = runs
    assert len({t.config_hash for t in v1.traces}) == 1
    assert len({t.config_hash for t in v2.traces}) == 1
    assert v1.traces[0].config_hash != v2.traces[0].config_hash


def test_replay_refuses_a_suite_missing_a_recorded_case(loaded):
    corpus, cases = loaded
    v1 = run_suite("grounded-v1", cases, corpus, PolicySet(), run_id="x")
    with pytest.raises(ValueError, match="missing from the suite"):
        replay(v1, "grounded-v2", cases[:-1], corpus, PolicySet())


def test_cli_record_inspect_replay_and_compare(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    out = str(tmp_path / "out")

    assert main(["record", "--agent", "grounded-v1", "--out", out, "--quiet"]) == 0
    runs_dir = Path(out) / "runs"
    recorded = sorted(runs_dir.glob("*.jsonl"))
    assert recorded
    run_id = recorded[0].stem

    assert load_traces(recorded[0])
    assert main(["inspect", "--run", run_id, "--out", out, "--failures", "--no-color"]) == 0
    assert main(["replay", "--run", run_id, "--agent", "grounded-v2",
                 "--out", out, "--no-color"]) == 0

    after = [p for p in sorted(runs_dir.glob("*.jsonl")) if p.stem != run_id]
    assert after
    assert main(["compare", "--before", run_id, "--after", after[0].stem,
                 "--out", out, "--no-color"]) == 0
    assert list((Path(out) / "scores").glob("*.json"))


def test_cli_validate_passes(monkeypatch):
    monkeypatch.chdir(ROOT)
    assert main(["validate"]) == 0


def test_cli_demo_runs_the_whole_story(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert main(["demo", "--out", str(tmp_path / "demo"), "--no-color"]) == 0

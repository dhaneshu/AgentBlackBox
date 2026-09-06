"""Verdict logic, run metrics, and the fixed/regressed comparison."""

from __future__ import annotations

from blackbox.replay import Run
from blackbox.score import compare, judge, score_run
from blackbox.suite import Case
from blackbox.trace import Citation, Trace

ANSWER = "Production go-live is 15 October 2026."


def case(**overrides) -> Case:
    data = {
        "id": "Q-1",
        "question": "When does production go live?",
        "expect": "answer",
        "must_contain": ("15 October",),
        "must_cite": ("plan.md",),
    }
    data.update(overrides)
    return Case(**data)


def trace(**overrides) -> Trace:
    result = Trace(
        run_id="r", case_id="Q-1", agent="test",
        question="When does production go live?", answer=ANSWER,
    )
    result.citations = [
        Citation("samples/corpus/plan.md", (11, 11), ANSWER, ANSWER, 1.0)
    ]
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


def test_a_grounded_correct_answer_is_correct():
    assert judge(trace(), case()).verdict == "correct"


def test_refusing_an_answerable_question_is_over_refusal():
    result = judge(trace(refused=True, answer="", citations=[]), case())
    assert result.verdict == "over_refused"


def test_refusing_an_unanswerable_question_is_correct():
    unanswerable = case(expect="refuse", must_contain=(), must_cite=())
    result = judge(trace(refused=True, answer="", citations=[]), unanswerable)
    assert result.verdict == "correctly_refused"


def test_answering_an_unanswerable_question_is_hallucination():
    unanswerable = case(expect="refuse", must_contain=(), must_cite=())
    assert judge(trace(), unanswerable).verdict == "hallucinated"


def test_an_answer_whose_citation_does_not_carry_it_is_hallucination():
    bad = trace()
    bad.citations = [Citation("samples/corpus/plan.md", (11, 11), "unrelated", ANSWER, 0.0)]
    assert judge(bad, case()).verdict == "hallucinated"


def test_a_missing_required_phrase_is_wrong_not_hallucinated():
    result = judge(trace(), case(must_contain=("2 October",)))
    assert result.verdict == "wrong"
    assert "missing required content" in result.reason


def test_forbidden_content_makes_the_answer_wrong():
    result = judge(trace(), case(forbid=("15 October",)))
    assert result.verdict == "wrong"


def test_citing_the_wrong_source_is_wrong():
    result = judge(trace(), case(must_cite=("sow.md",)))
    assert result.verdict == "wrong"
    assert "Expected a citation" in result.reason


def test_a_refusal_counts_as_grounded_because_it_asserted_nothing():
    assert trace(refused=True, answer="", citations=[]).grounded is True


def test_run_metrics_match_hand_calculation():
    cases = [case(id="Q-1"), case(id="Q-2"), case(id="Q-3", expect="refuse",
                                                 must_contain=(), must_cite=())]
    traces = [
        trace(case_id="Q-1"),                                    # correct
        trace(case_id="Q-2", refused=True, answer="", citations=[]),  # over-refused
        trace(case_id="Q-3", refused=True, answer="", citations=[]),  # correctly refused
    ]
    result = score_run(Run("r", "test", traces), cases)

    assert result.total == 3
    assert result.count("correct") == 1
    assert result.count("over_refused") == 1
    assert result.count("correctly_refused") == 1
    assert result.accuracy == 2 / 3
    assert result.over_refusal_rate == 1 / 3
    assert result.hallucination_rate == 0.0


def test_traces_without_a_matching_case_are_ignored():
    result = score_run(Run("r", "test", [trace(case_id="Q-99")]), [case(id="Q-1")])
    assert result.total == 0


def test_comparison_labels_a_fix_and_a_regression():
    cases = [
        case(id="Q-1"),
        case(id="Q-2", expect="refuse", must_contain=(), must_cite=()),
    ]
    before = score_run(
        Run("a", "v1", [
            trace(case_id="Q-1"),                 # correct
            trace(case_id="Q-2"),                 # hallucinated
        ]), cases,
    )
    after = score_run(
        Run("b", "v2", [
            trace(case_id="Q-1", refused=True, answer="", citations=[]),  # over-refused
            trace(case_id="Q-2", refused=True, answer="", citations=[]),  # correctly refused
        ]), cases,
    )

    result = compare(before, after)
    assert [c.case_id for c in result.fixed] == ["Q-2"]
    assert [c.case_id for c in result.regressed] == ["Q-1"]
    assert result.net == 0


def test_an_unchanged_verdict_is_neither_fixed_nor_regressed():
    cases = [case(id="Q-1")]
    before = score_run(Run("a", "v1", [trace(case_id="Q-1")]), cases)
    after = score_run(Run("b", "v2", [trace(case_id="Q-1")]), cases)
    result = compare(before, after)
    assert result.fixed == [] and result.regressed == []
    assert len(result.of_kind("unchanged")) == 1


def test_comparison_only_covers_cases_present_in_both_runs():
    cases = [case(id="Q-1"), case(id="Q-2")]
    before = score_run(Run("a", "v1", [trace(case_id="Q-1"), trace(case_id="Q-2")]), cases)
    after = score_run(Run("b", "v2", [trace(case_id="Q-1")]), cases)
    assert len(compare(before, after).changes) == 1

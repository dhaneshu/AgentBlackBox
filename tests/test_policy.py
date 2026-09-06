"""Policies: does each one fire on the run it was written for, and only then."""

from __future__ import annotations

from blackbox.policy import POLICIES, PolicySet, apply, check
from blackbox.trace import Citation, Step, Trace


def build(**overrides) -> Trace:
    trace = Trace(
        run_id="r", case_id="Q-1", agent="test", question="When is go-live?",
        answer="Production go-live is 15 October 2026.",
    )
    trace.steps.append(
        Step(1, "retrieve", "corpus.search", "", {"hits": [{"citation": "p.md:1", "score": 0.9}]})
    )
    trace.citations.append(
        Citation("p.md", (1, 1), "Production go-live is 15 October 2026.",
                 "Production go-live is 15 October 2026.", 1.0)
    )
    for key, value in overrides.items():
        setattr(trace, key, value)
    return trace


def fired(trace: Trace, policies: PolicySet | None = None) -> set[str]:
    return {v.policy for v in check(trace, policies or PolicySet())}


def test_a_clean_grounded_answer_breaks_nothing():
    assert fired(build()) == set()


def test_answering_with_no_citation_is_critical():
    trace = build(citations=[])
    violations = check(trace, PolicySet())
    assert {v.policy for v in violations} == {"UNCITED_ANSWER"}
    assert violations[0].severity == "critical"


def test_a_citation_that_does_not_carry_the_claim_is_critical():
    trace = build()
    trace.citations = [
        Citation("p.md", (1, 1), "Escalations are raised through the delivery lead.",
                 "The penalty is 5% of monthly fees.", 0.0)
    ]
    violations = check(trace, PolicySet())
    assert "UNSUPPORTED_CITATION" in {v.policy for v in violations}
    assert violations[0].severity == "critical"


def test_answering_below_the_evidence_floor_is_flagged():
    trace = build()
    trace.steps = [
        Step(1, "retrieve", "corpus.search", "", {"hits": [{"citation": "p.md:1", "score": 0.10}]})
    ]
    assert "ANSWERED_WITHOUT_EVIDENCE" in fired(trace)


def test_a_refusal_is_never_penalised_for_lacking_evidence():
    trace = build(refused=True, answer="", citations=[])
    trace.steps = [
        Step(1, "retrieve", "corpus.search", "", {"hits": [{"citation": "p.md:1", "score": 0.0}]})
    ]
    assert fired(trace) == set()


def test_a_tool_outside_the_allowlist_is_critical():
    trace = build()
    trace.steps.append(Step(2, "tool", "shell.exec", "rm -rf /", {}))
    violations = check(trace, PolicySet(allowed_tools=("corpus.search",)))
    assert "TOOL_NOT_ALLOWED" in {v.policy for v in violations}


def test_an_allowed_tool_passes():
    trace = build()
    trace.steps.append(Step(2, "tool", "corpus.search", "ok", {}))
    assert "TOOL_NOT_ALLOWED" not in fired(trace)


def test_an_email_address_in_the_answer_is_flagged():
    trace = build(answer="Contact priya.raman@northwind.example about go-live.")
    assert "PII_IN_OUTPUT" in fired(trace)


def test_a_phone_number_in_the_answer_is_flagged():
    trace = build(answer="Call +44 20 7946 0000 for support.")
    assert "PII_IN_OUTPUT" in fired(trace)


def test_pii_checking_can_be_switched_off():
    trace = build(answer="Contact priya@northwind.example.")
    assert "PII_IN_OUTPUT" not in fired(trace, PolicySet(forbid_pii=False))


def test_exceeding_the_token_budget_is_flagged():
    trace = build(tokens_in=600, tokens_out=600)
    assert "TOKEN_BUDGET_EXCEEDED" in fired(trace, PolicySet(token_budget=1000))


def test_a_zero_budget_disables_the_token_check():
    trace = build(tokens_in=10_000)
    assert "TOKEN_BUDGET_EXCEEDED" not in fired(trace, PolicySet(token_budget=0))


def test_violations_are_ordered_worst_first():
    trace = build(citations=[], tokens_in=5000)
    trace.steps = [
        Step(1, "retrieve", "corpus.search", "", {"hits": [{"citation": "p.md:1", "score": 0.0}]})
    ]
    severities = [v.severity for v in check(trace, PolicySet(token_budget=10))]
    order = ["critical", "high", "medium", "low"]
    assert severities == sorted(severities, key=order.index)


def test_apply_attaches_violations_and_records_that_the_check_ran():
    trace = apply(build(citations=[]), PolicySet())
    assert trace.violations
    assert trace.steps[-1].kind == "policy"
    assert "policies" in trace.config


def test_every_policy_that_can_fire_is_documented():
    trace = build(citations=[], answer="mail me at a@b.co", tokens_in=9999)
    trace.steps = [
        Step(1, "retrieve", "corpus.search", "", {"hits": [{"citation": "p.md:1", "score": 0.0}]}),
        Step(2, "tool", "shell.exec", "", {}),
    ]
    assert fired(trace, PolicySet(token_budget=1)) <= set(POLICIES)

"""Phase 3 dataset, assertion, and fixture contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import jsonschema

from blackbox.assertions import AssertionEngine, JSONPathError, select
from blackbox.domain.result import judge
from blackbox.domain.suite import (
    Assertion, Dataset, EvaluationCase, FixtureRef, load_dataset,
)
from blackbox.domain.schema import migrate_document
from blackbox.domain.trace import (
    ArtifactRef, Cost, Message, Span, ToolRequestEvent, ToolResponseEvent, Trace, Usage,
    Violation, Citation,
)
from blackbox.fixtures import (
    DestructiveFixtureApprovalRequired, FilesystemSandboxFixtureProvider,
    FixtureError, FixtureManager, HTTPFixtureProvider, InMemoryFixtureProvider,
    SQLReadOnlyFixtureProvider, UnsafeFixtureTarget,
)
from blackbox.replay import replay, run_suite


def case(identifier: str = "c1", tag: str = "smoke") -> EvaluationCase:
    return EvaluationCase(
        identifier, (Message("system", "be concise"), Message("user", "hello {{name}}")),
        variables={"name": "world"}, tags=(tag,), expected_outcome="answer",
        assertions=(Assertion("text_contains", "hello"),), timeout_seconds=3,
    )


def rich_trace() -> Trace:
    trace = Trace("run", "c1", "agent", "question", answer='{"score": 10}', refused=False)
    tool = Span("lookup", trace.trace_id, trace.conversation_id, trace.turn_id,
                kind="tool", duration_ns=2_000_000, usage=Usage(1, 2),
                cost=Cost("0.02"))
    tool.events.extend([
        ToolRequestEvent("request", {"arguments": {"id": 7}}),
        ToolResponseEvent("response", {"result": {"name": "Ada"}}),
    ])
    trace.spans.append(tool)
    trace.usage = Usage(3, 4, tool_calls=1)
    trace.cost = Cost("0.02")
    trace.violations.append(Violation("blocked", "high", "reason"))
    trace.artifacts.append(ArtifactRef("memory://x", "abc", name="output"))
    return trace


def test_legacy_case_migrates_to_ordered_evaluation_case():
    value = EvaluationCase.from_dict({
        "id": "legacy", "question": "where?", "expect": "answer",
        "must_contain": ["here"], "must_cite": ["doc.md"], "notes": "old",
    })
    assert value.question == "where?"
    assert [a.type for a in value.assertions] == ["text_contains", "citation"]
    assert value.notes == "old"


def test_legacy_suite_migration_produces_schema_valid_document():
    migrated = migrate_document("suite", {
        "id": "legacy", "question": "where?", "expect": "answer",
        "must_contain": ["here"],
    })
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "suite-2.0.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(migrated)
    assert "question" not in migrated


def test_jsonpath_and_assertions_are_deterministic_and_evidence_rich():
    assert select({"a": [{"b": 3}]}, "$.a[0].b") == [3]
    assertions = [
        Assertion("json_schema", {"type": "object", "required": ["score"]}),
        Assertion("numeric_tolerance", 10, "$.answer", {"absolute": 0}),
        Assertion("tool_call_count", 1),
        Assertion("tool_args", {"id": 7}, options={"name": "lookup"}),
        Assertion("tool_result", {"name": "Ada"}, options={"name": "lookup"}),
        Assertion("latency", 3, options={"operator": "lte"}),
        Assertion("tokens", 7),
        Assertion("cost", "0.02", options={"operator": "eq"}),
        Assertion("policy_violation", {"policy": "blocked"}),
        Assertion("artifact_digest", "abc", options={"name": "output"}),
    ]
    results = AssertionEngine().evaluate(assertions, rich_trace())
    # Numeric selection deliberately receives JSON text, yielding an explicit error.
    assert [r.status for r in results] == [
        "pass", "error", "pass", "pass", "pass", "pass", "pass", "pass", "pass", "pass"
    ]
    assert results[1].failure_code == "NON_NUMERIC_VALUE"
    assert results[2].evidence_refs


def test_jsonpath_preserves_unicode_and_decodes_supported_escapes():
    document = {
        "café": 1,
        "line\nbreak": 2,
        "emoji😀": 3,
        "single'quote": 4,
        'double"quote': 5,
        r"slash\key": 6,
    }
    assert select(document, "$['café']") == [1]
    assert select(document, r'$["line\nbreak"]') == [2]
    assert select(document, r"$['emoji\uD83D\uDE00']") == [3]
    assert select(document, r"$['single\'quote']") == [4]
    assert select(document, r'$["double\"quote"]') == [5]
    assert select(document, r"$['slash\\key']") == [6]
    with pytest.raises(JSONPathError, match="unsupported quoted-key escape"):
        select(document, r"$['bad\q']")


def test_latency_uses_interval_union_instead_of_nested_span_sum():
    trace = rich_trace()
    trace.spans = [
        Span("parent", trace.trace_id, trace.conversation_id, trace.turn_id,
             start_ns=1_000_000, end_ns=11_000_000, duration_ns=10_000_000),
        Span("child", trace.trace_id, trace.conversation_id, trace.turn_id,
             start_ns=3_000_000, end_ns=8_000_000, duration_ns=5_000_000),
        Span("later", trace.trace_id, trace.conversation_id, trace.turn_id,
             start_ns=20_000_000, end_ns=22_000_000, duration_ns=2_000_000),
    ]
    result = AssertionEngine().evaluate(
        [Assertion("latency", 12, options={"operator": "eq"})], trace
    )[0]
    assert result.passed
    assert result.actual == 12


def test_latency_unions_partial_overlap_and_uses_max_for_legacy_durations():
    trace = rich_trace()
    trace.spans = [
        Span("first", trace.trace_id, trace.conversation_id, trace.turn_id,
             start_ns=1_000_000, end_ns=5_000_000),
        Span("second", trace.trace_id, trace.conversation_id, trace.turn_id,
             start_ns=3_000_000, end_ns=8_000_000),
    ]
    overlap = AssertionEngine().evaluate([Assertion("latency", 7)], trace)[0]
    assert overlap.actual == 7

    trace.spans = [
        Span("long", trace.trace_id, trace.conversation_id, trace.turn_id,
             duration_ns=5_000_000),
        Span("nested", trace.trace_id, trace.conversation_id, trace.turn_id,
             duration_ns=2_000_000),
    ]
    legacy = AssertionEngine().evaluate([Assertion("latency", 5)], trace)[0]
    assert legacy.actual == 5


def test_scoring_runs_all_assertions_and_persists_failure_evidence():
    trace = Trace("run", "case", "agent", "question", answer="supported answer")
    trace.citations = [
        Citation("source.md", (1, 1), "supported answer", "supported answer", 1.0)
    ]
    evaluation_case = EvaluationCase(
        "case", (Message("user", "question"),), expected_outcome="answer",
        assertions=(Assertion("regex", r"^missing$", assertion_id="shape"),),
    )
    result = judge(trace, evaluation_case)
    assert result.verdict == "wrong"
    assert result.assertion_results[0].failure_code == "REGEX_NO_MATCH"
    serialized = result.to_dict()
    assert serialized["assertion_results"][0]["assertion_id"] == "shape"
    assert serialized["assertion_results"][0]["evidence_refs"]


def test_scoring_integrates_every_supported_assertion_type():
    trace = rich_trace()
    trace.citations = [
        Citation("source.md", (1, 1), trace.answer, trace.answer, 1.0)
    ]
    assertions = (
        Assertion("text_exact", trace.answer),
        Assertion("text_contains", "score"),
        Assertion("text_not_contains", "missing"),
        Assertion("regex", r'"score":\s*10'),
        Assertion("refusal", False),
        Assertion("json_schema", {"type": "object", "required": ["score"]}),
        Assertion("numeric_tolerance", 3, "$.usage.input_tokens"),
        Assertion("citation", "source.md"),
        Assertion("span_exists", "lookup"),
        Assertion("span_sequence", ["lookup"]),
        Assertion("tool_call_count", 1),
        Assertion("tool_args", {"id": 7}, options={"name": "lookup"}),
        Assertion("tool_result", {"name": "Ada"}, options={"name": "lookup"}),
        Assertion("latency", 2, options={"operator": "lte"}),
        Assertion("tokens", 7),
        Assertion("cost", "0.02", options={"operator": "eq"}),
        Assertion("policy_violation", {"policy": "blocked"}),
        Assertion("artifact_digest", "abc", options={"name": "output"}),
    )
    evaluation_case = EvaluationCase(
        "c1", (Message("user", "question"),), expected_outcome="answer",
        assertions=assertions,
    )
    result = judge(trace, evaluation_case)
    assert len(result.assertion_results) == len(assertions)
    assert all(assertion_result.passed for assertion_result in result.assertion_results), [
        assertion_result.to_dict() for assertion_result in result.assertion_results
        if not assertion_result.passed
    ]
    assert result.verdict == "correct"


def test_dataset_formats_composition_filter_hash_and_seed(tmp_path):
    included = tmp_path / "included.jsonl"
    included.write_text(json.dumps({
        "id": "base", "question": "base", "expect": "answer",
        "must_contain": ["base"], "tags": ["base"],
    }), encoding="utf-8")
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        "include: [included.jsonl]\nparameters:\n  - {name: Ada}\n  - {name: Grace}\n"
        "cases:\n  - id: greeting\n    messages:\n      - {role: user, content: 'Hi {{name}}'}\n"
        "    tags: [smoke]\n    expected_outcome: answer\n"
        "    assertions:\n      - {type: text_contains, expected: '{{name}}'}\n",
        encoding="utf-8",
    )
    dataset = load_dataset(suite)
    assert len(dataset.cases) == 3
    assert dataset.version_hash == load_dataset(suite).version_hash
    assert len(dataset.filter(include_tags=["smoke"]).cases) == 2
    assert dataset.sample(size=2, seed=7).selected_case_ids == dataset.sample(
        size=2, seed=7
    ).selected_case_ids
    split = dataset.split({"train": 2, "test": 1}, seed=4)
    assert sum(len(value.cases) for value in split.values()) == 3
    assert split["train"].sampling.selected_case_ids == split["train"].selected_case_ids


def test_filter_updates_sampling_ids_and_preserves_sampling_provenance():
    dataset = Dataset(tuple(case(f"c{i}", "keep" if i % 2 else "drop")
                            for i in range(6)))
    sampled = dataset.sample(size=5, seed=11)
    filtered = sampled.filter(include_tags=["keep"])
    assert filtered.sampling.selected_case_ids == filtered.selected_case_ids
    assert filtered.sampling.provenance_selected_case_ids == sampled.selected_case_ids
    chained = filtered.filter(case_ids=filtered.selected_case_ids[:1])
    assert chained.sampling.selected_case_ids == chained.selected_case_ids
    assert chained.sampling.provenance_selected_case_ids == sampled.selected_case_ids


def test_fixture_lifecycle_approval_and_snapshot_refs():
    provider = InMemoryFixtureProvider({"value": 1}, destructive=True)
    manager = FixtureManager({"memory": provider})
    with pytest.raises(DestructiveFixtureApprovalRequired):
        manager.run("state", "memory", {}, lambda state: state.update(value=2))
    execution = manager.run(
        "state", "memory", {"expected": {"value": 2}},
        lambda state: state.update(value=2), approve_destructive=True,
    )
    assert execution.before.digest != execution.after.digest
    assert execution.verification["passed"]


class _RefusingAgent:
    name = "refusing-test"

    def config(self):
        return {}

    def answer(self, question, corpus, recorder):
        recorder.refused("test")


class _TrackingFixture:
    destructive = True

    def __init__(self):
        self.snapshots = 0
        self.cleaned = 0

    def prepare(self, config):
        return {}

    def snapshot(self, state):
        self.snapshots += 1
        return {"snapshot": self.snapshots}

    def verify(self, before, after, config):
        return {"passed": after["snapshot"] > before["snapshot"],
                "expected": "changed", "actual": after}

    def cleanup(self, state):
        self.cleaned += 1


def test_run_and_replay_integrate_fixture_evidence_cleanup_and_approval():
    provider = _TrackingFixture()
    manager = FixtureManager({"tracking": provider})
    evaluation_case = EvaluationCase(
        "fixture-case", (Message("user", "unknown"),),
        fixture_refs=(FixtureRef("tracking", name="state", destructive=True),),
        expected_outcome="refuse", assertions=(Assertion("refusal", True),),
    )
    with pytest.raises(DestructiveFixtureApprovalRequired):
        run_suite(_RefusingAgent(), [evaluation_case], None, fixture_manager=manager)
    assert provider.cleaned == 0  # Approval is checked before prepare.

    run = run_suite(
        _RefusingAgent(), [evaluation_case], None, fixture_manager=manager,
        approve_destructive_fixtures=True, run_id="fixture-run",
    )
    assert provider.cleaned == 1
    assert len(run.traces[0].artifacts) == 2
    assert run.traces[0].steps[-1].name == "fixture.verify"
    scored = judge(run.traces[0], evaluation_case)
    assert scored.verdict == "correctly_refused"
    assert scored.assertion_results[-1].assertion_type == "fixture_verification"

    replay_provider = _TrackingFixture()
    replayed = replay(
        run, _RefusingAgent(), [evaluation_case], None,
        fixture_manager=FixtureManager({"tracking": replay_provider}),
        approve_destructive_fixtures=True,
    )
    assert replay_provider.cleaned == 1
    assert len(replayed.traces[0].artifacts) == 2


def test_failed_fixture_verification_affects_the_case_verdict():
    class FailingVerification(_TrackingFixture):
        destructive = False

        def verify(self, before, after, config):
            return {"passed": False, "expected": "unchanged", "actual": after}

    provider = FailingVerification()
    evaluation_case = EvaluationCase(
        "fixture-case", (Message("user", "unknown"),),
        fixture_refs=(FixtureRef("tracking"),), expected_outcome="refuse",
        assertions=(Assertion("refusal", True),),
    )
    run = run_suite(
        _RefusingAgent(), [evaluation_case], None,
        fixture_manager=FixtureManager({"tracking": provider}),
    )
    result = judge(run.traces[0], evaluation_case)
    assert result.verdict == "wrong"
    assert result.assertion_results[-1].failure_code == "FIXTURE_VERIFICATION_FAILED"
    assert provider.cleaned == 1


def test_fixture_cleanup_runs_when_snapshot_or_operation_fails():
    class Broken(_TrackingFixture):
        destructive = False

        def snapshot(self, state):
            raise FixtureError("snapshot failed")

    broken = Broken()
    with pytest.raises(FixtureError, match="snapshot failed"):
        FixtureManager({"broken": broken}).run_many(
            [FixtureRef("broken")], lambda: None
        )
    assert broken.cleaned == 1

    working = _TrackingFixture()
    working.destructive = False

    def fail_operation():
        raise RuntimeError("agent failed")

    with pytest.raises(RuntimeError, match="agent failed"):
        FixtureManager({"working": working}).run_many(
            [FixtureRef("working")], fail_operation
        )
    assert working.cleaned == 1


def test_multiple_fixtures_wrap_one_operation_and_cleanup_in_reverse_order():
    cleanup_order = []

    class Ordered(_TrackingFixture):
        destructive = False

        def __init__(self, name):
            super().__init__()
            self.name = name

        def cleanup(self, state):
            super().cleanup(state)
            cleanup_order.append(self.name)

    first, second = Ordered("first"), Ordered("second")
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        return "done"

    result, executions = FixtureManager({
        "first": first, "second": second,
    }).run_many([FixtureRef("first"), FixtureRef("second")], operation)
    assert result == "done"
    assert calls == 1
    assert len(executions) == 2
    assert cleanup_order == ["second", "first"]


def test_cleanup_failure_does_not_hide_the_primary_failure():
    class CleanupFails(_TrackingFixture):
        destructive = False

        def cleanup(self, state):
            raise FixtureError("cleanup failed")

    with pytest.raises(ExceptionGroup) as raised:
        FixtureManager({"fixture": CleanupFails()}).run_many(
            [FixtureRef("fixture")],
            lambda: (_ for _ in ()).throw(RuntimeError("agent failed")),
        )
    assert [str(error) for error in raised.value.exceptions] == [
        "agent failed", "cleanup failed",
    ]


def test_http_fixture_rejects_dns_and_non_public_literal_targets():
    provider = HTTPFixtureProvider()
    with pytest.raises(UnsafeFixtureTarget, match="literal public IP"):
        provider.prepare({"url": "https://example.com/state"})
    with pytest.raises(UnsafeFixtureTarget, match="public address"):
        provider.prepare({"url": "https://127.0.0.1/state"})
    with pytest.raises(UnsafeFixtureTarget, match="HTTPS"):
        provider.prepare({"url": "http://93.184.216.34/state"})
    with pytest.raises(UnsafeFixtureTarget, match="credentials"):
        provider.prepare({"url": "https://user:secret@93.184.216.34/state"})
    with pytest.raises(UnsafeFixtureTarget, match="credential headers"):
        provider.prepare({
            "url": "https://93.184.216.34/state",
            "headers": {"Authorization": "Bearer secret"},
        })
    state = provider.prepare({"url": "https://93.184.216.34/state"})
    assert state["url"] == "https://93.184.216.34/state"
    ipv6 = provider.prepare({"url": "https://[2606:4700:4700::1111]/state"})
    assert ipv6["url"] == "https://[2606:4700:4700::1111]/state"


def test_filesystem_and_sql_providers_confine_and_enforce_read_only(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "item.txt").write_text("value", encoding="utf-8")
    fs = FilesystemSandboxFixtureProvider(sandbox)
    assert fs.snapshot(fs.prepare({"path": "item.txt"}))["type"] == "file"
    with pytest.raises(UnsafeFixtureTarget):
        fs.prepare({"path": "..\\outside.txt"})

    database = tmp_path / "fixture.db"
    with sqlite3.connect(database) as connection:
        connection.execute("create table items(value text)")
        connection.execute("insert into items values ('safe')")
    sql = SQLReadOnlyFixtureProvider()
    assert sql.snapshot(sql.prepare({
        "database": str(database), "query": "select value from items"
    }))["rows"] == [["safe"]]
    with pytest.raises(FixtureError, match="read-only"):
        sql.prepare({"database": str(database), "query": "delete from items"})

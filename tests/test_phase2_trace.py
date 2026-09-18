from __future__ import annotations

import asyncio
import json
import re

import jsonschema
import pytest

from blackbox.domain.trace import (
    AgentHandoffEvent,
    ArtifactRef,
    Conversation,
    Cost,
    CustomEvent,
    ErrorEvent,
    EvaluatorResultEvent,
    HumanReviewEvent,
    Message,
    ModelRequestEvent,
    ModelResponseEvent,
    PayloadTooLargeError,
    PolicyDecisionEvent,
    RetrievalEvent,
    Span,
    ToolRequestEvent,
    ToolResponseEvent,
    Trace,
    TraceCorruptionError,
    TraceValidationError,
    Turn,
    Usage,
    canonical_json,
    content_digest,
    event_from_dict,
    iter_traces,
    load_traces,
    new_ulid,
    save_traces,
)
from blackbox.recorder import Recorder


def test_ulid_generation_and_w3c_id_ingestion():
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", new_ulid())
    w3c_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    w3c_span = "00f067aa0ba902b7"
    conversation = Conversation("conversation")
    turn = Turn("conversation", "agent", "turn")
    conversation.turns.append(turn)
    span = Span("work", w3c_trace, "conversation", "turn", w3c_span)
    turn.span_ids.append(w3c_span)
    trace = Trace("run", "case", "agent", "question", trace_id=w3c_trace,
                  conversations=[conversation], spans=[span])
    assert Trace.from_dict(trace.to_dict()).spans[0].span_id == w3c_span


def test_typed_events_round_trip_by_discriminator():
    event_types = [
        AgentHandoffEvent, RetrievalEvent, ModelRequestEvent, ModelResponseEvent,
        ToolRequestEvent, ToolResponseEvent, PolicyDecisionEvent,
        EvaluatorResultEvent, HumanReviewEvent, ErrorEvent, CustomEvent,
    ]
    restored = [event_from_dict(cls("operation", {"value": 1}).to_dict())
                for cls in event_types]
    assert [type(value) for value in restored] == event_types
    with pytest.raises(TraceValidationError, match="unknown event"):
        event_from_dict({"type": "future", "name": "nope"})


def test_value_objects_validation_and_serialization():
    artifact = ArtifactRef("file:///evidence", "sha256:abc", size_bytes=3)
    message = Message("tool", {"ok": True}, artifact_refs=(artifact,))
    assert Message.from_dict(message.to_dict()) == message
    assert Usage(2, 3).total_tokens == 5
    assert Cost("0.0100").to_dict()["amount"] == "0.0100"
    with pytest.raises(TraceValidationError, match="negative"):
        Usage(input_tokens=-1)
    with pytest.raises(TraceValidationError, match="negative"):
        Cost("-1")


def test_sync_contexts_capture_nanosecond_timing_and_exceptions():
    recorder = Recorder("run", "case", "agent", "question")
    with recorder.conversation("title") as conversation:
        with recorder.turn() as turn:
            with pytest.raises(RuntimeError, match="boom"):
                with recorder.span("tool", kind="tool") as span:
                    recorder.message("user", "hello")
                    raise RuntimeError("boom")

    assert conversation.status == "completed"
    assert turn.status == "completed"
    assert span.status == "error"
    assert span.duration_ns > 0
    assert span.error and span.error.type == "RuntimeError"
    assert isinstance(span.events[-1], ErrorEvent)
    recorder.finish()


def test_async_contextvars_preserve_parent_for_concurrent_spans():
    async def run():
        recorder = Recorder("run", "case", "coordinator", "question")
        async with recorder.aconversation("parallel"):
            async with recorder.aturn():
                async with recorder.aspan("root", kind="agent") as root:
                    async def worker(index: int):
                        await asyncio.sleep(0)
                        async with recorder.aspan(f"tool-{index}", kind="tool") as child:
                            await asyncio.sleep(0)
                            recorder.event(ToolResponseEvent("done", {"index": index}))
                            return child

                    children = await asyncio.gather(*(worker(i) for i in range(8)))
        return recorder.finish(), root, children

    trace, root, children = asyncio.run(run())
    assert {child.parent_span_id for child in children} == {root.span_id}
    assert len({child.span_id for child in children}) == 8
    assert all(child.status == "ok" and child.duration_ns > 0 for child in children)
    assert len(trace.spans) == 9


def test_asyncio_cancellation_marks_all_contexts_cancelled_without_error_event():
    async def run():
        recorder = Recorder("run", "case", "agent", "question")
        try:
            async with recorder.conversation() as conversation:
                async with recorder.turn() as turn:
                    async with recorder.span("cancelled") as span:
                        raise asyncio.CancelledError()
        except asyncio.CancelledError:
            pass
        return recorder, conversation, turn, span

    recorder, conversation, turn, span = asyncio.run(run())
    assert (conversation.status, turn.status, span.status) == (
        "cancelled", "cancelled", "cancelled"
    )
    assert span.error and span.error.type == "CancelledError"
    assert not any(isinstance(event, ErrorEvent) for event in span.events)
    recorder.finish()


def test_graph_rejects_orphans_wrong_owners_and_cycles():
    conversation = Conversation("conversation")
    conversation.turns.append(Turn("conversation", "agent", "turn"))
    trace = Trace("run", "case", "agent", "question",
                  conversations=[conversation])
    trace.spans.append(Span("orphan", trace.trace_id, "conversation", "turn",
                            parent_span_id="missing"))
    with pytest.raises(TraceValidationError, match="parent not found"):
        trace.validate()

    first = Span("first", trace.trace_id, "conversation", "turn")
    second = Span("second", trace.trace_id, "conversation", "turn",
                  parent_span_id=first.span_id)
    first.parent_span_id = second.span_id
    trace.spans = [first, second]
    conversation.turns[0].span_ids = [first.span_id, second.span_id]
    with pytest.raises(TraceValidationError, match="cyclic"):
        trace.validate()

    first.parent_span_id = None
    first.trace_id = "another-trace"
    trace.spans = [first]
    conversation.turns[0].span_ids = [first.span_id]
    with pytest.raises(TraceValidationError, match="another trace"):
        trace.validate()


def test_turn_span_ids_must_exactly_match_owned_spans():
    conversation = Conversation("conversation")
    turn = Turn("conversation", "agent", "turn")
    conversation.turns.append(turn)
    span = Span("work", "trace", "conversation", "turn", "span")
    trace = Trace("run", "case", "agent", "question", trace_id="trace",
                  conversations=[conversation], spans=[span])
    with pytest.raises(TraceValidationError, match="exactly match"):
        trace.validate()
    turn.span_ids = ["span", "extra"]
    with pytest.raises(TraceValidationError, match="exactly match"):
        trace.validate()
    turn.span_ids = ["span", "span"]
    with pytest.raises(TraceValidationError, match="duplicate span_ids"):
        trace.validate()
    turn.span_ids = ["span"]
    trace.validate()


def test_handoff_links_agents_payload_and_lifecycle():
    recorder = Recorder("run", "case", "coordinator", "question")
    payload = ArtifactRef("memory://handoff", content_digest({"task": "x"}),
                          "application/json")
    with recorder.conversation(participants=["coordinator", "worker"]):
        with recorder.turn("coordinator"):
            with recorder.span("source", kind="agent") as source:
                with recorder.span("target", kind="agent", agent_id="worker") as target:
                    pass
                handoff = recorder.handoff(target, "worker", payload_ref=payload)
                recorder.transition_handoff(handoff, "accepted")
                recorder.transition_handoff(handoff, "completed")
    trace = recorder.finish()
    assert handoff.status == "completed"
    assert handoff.payload_ref == payload
    assert target.links[0].span_id == source.span_id
    assert [event.name for event in source.events] == [
        "handoff.requested", "handoff.accepted", "handoff.completed"
    ]
    with pytest.raises(TraceValidationError, match="invalid handoff transition"):
        recorder.transition_handoff(handoff, "failed")
    assert trace.handoffs == [handoff]


def test_handoff_can_be_requested_before_target_work_and_tracks_lifecycle():
    recorder = Recorder("run", "case", "coordinator", "question")
    with recorder.conversation(participants=["coordinator", "worker"]):
        with recorder.turn("coordinator"):
            with recorder.span("source", kind="agent") as source:
                handoff = recorder.request_handoff("worker")
                assert handoff.status == "requested"
                assert handoff.target_span_id is None
                with pytest.raises(TraceValidationError, match="without a target"):
                    recorder.transition_handoff(handoff, "accepted")
                with recorder.accept_handoff(handoff, "worker-task") as target:
                    assert handoff.status == "accepted"
                    assert handoff.target_span_id == target.span_id
                assert handoff.status == "completed"
    recorder.finish()
    assert target.agent_id == "worker"
    assert target.links[0].span_id == source.span_id


def test_handoff_validates_source_and_target_agent_ownership():
    recorder = Recorder("run", "case", "coordinator", "question")
    with recorder.conversation():
        with recorder.turn():
            with recorder.span("source", kind="agent"):
                handoff = recorder.request_handoff("worker")
                with recorder.accept_handoff(handoff, "work"):
                    pass
    handoff.source_agent_id = "impostor"
    with pytest.raises(TraceValidationError, match="source agent"):
        recorder.trace.validate()
    handoff.source_agent_id = "coordinator"
    handoff.target_agent_id = "impostor"
    with pytest.raises(TraceValidationError, match="target agent"):
        recorder.trace.validate()


def test_canonical_serialization_digest_and_jsonl_streaming(tmp_path):
    first = {"b": [2, 1], "a": {"z": False, "x": "é"}}
    second = {"a": {"x": "é", "z": False}, "b": [2, 1]}
    assert canonical_json(first) == canonical_json(second)
    assert content_digest(first) == content_digest(second)

    traces = (Trace(f"run-{i}", "case", "agent", "question") for i in range(3))
    path = save_traces(traces, tmp_path / "stream.jsonl")
    iterator = iter_traces(path)
    assert iter(iterator) is iterator
    assert [trace.run_id for trace in iterator] == ["run-0", "run-1", "run-2"]
    assert load_traces(path)[0].to_dict() == load_traces(path)[0].to_dict()


def test_payload_limit_and_corruption_diagnostics(tmp_path):
    recorder = Recorder("run", "case", "agent", "question", payload_limit_bytes=64)
    with recorder.conversation():
        with recorder.turn():
            with recorder.span("tool"):
                with pytest.raises(PayloadTooLargeError) as caught:
                    recorder.event(CustomEvent("large", {"value": "x" * 100}))
    assert caught.value.actual_bytes > caught.value.limit_bytes
    assert caught.value.identifier

    path = tmp_path / "corrupt.jsonl"
    path.write_text('{"event_id":"evt-7",broken}\n', encoding="utf-8")
    with pytest.raises(TraceCorruptionError) as corrupted:
        list(iter_traces(path))
    assert corrupted.value.line == 1
    assert str(path) in str(corrupted.value)


def test_artifact_metadata_limits_apply_at_every_serialized_location():
    def rich_trace():
        recorder = Recorder("run", "case", "agent", "question")
        with recorder.conversation():
            with recorder.turn():
                with recorder.span("source", kind="agent") as source:
                    with recorder.span("target", kind="agent", agent_id="worker") as target:
                        pass
                    handoff = recorder.handoff(target, "worker")
                    message = recorder.message("user", "hi")
                    event = CustomEvent("event")
                    source.events.append(event)
        return recorder.trace, handoff, message, event

    for location in ("top", "message", "event", "handoff"):
        trace, handoff, message, event = rich_trace()
        artifact = ArtifactRef("x" * 1000, "digest")
        if location == "top":
            trace.artifacts.append(artifact)
        elif location == "message":
            object.__setattr__(message, "artifact_refs", (artifact,))
        elif location == "event":
            object.__setattr__(event, "artifact_refs", (artifact,))
        else:
            handoff.payload_ref = artifact
        trace.payload_limit_bytes = 512
        with pytest.raises(PayloadTooLargeError) as caught:
            trace.validate()
        assert caught.value.identifier == artifact.artifact_id


@pytest.mark.parametrize("document", [
    [],
    {"spans": {}},
    {"usage": []},
    {"conversations": [{"turns": {}}]},
    {"spans": [{"links": "bad", "events": []}]},
])
def test_iter_traces_shape_errors_are_corruption_errors(tmp_path, document):
    path = tmp_path / "shape.jsonl"
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    with pytest.raises(TraceCorruptionError) as caught:
        list(iter_traces(path))
    assert caught.value.path == path
    assert caught.value.line == 1


def test_iter_traces_recovers_nested_event_id_for_conversion_error(tmp_path):
    document = Trace("run", "case", "agent", "question").to_dict()
    document["spans"] = [{
        "events": [{"event_id": "event-42", "type": "custom", "payload": []}],
        "links": [],
    }]
    path = tmp_path / "event-error.jsonl"
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    with pytest.raises(TraceCorruptionError) as caught:
        list(iter_traces(path))
    assert caught.value.event_id == "event-42"
    assert "event-42" in str(caught.value)


@pytest.mark.parametrize(("model", "field", "value", "message"), [
    ("conversation", "status", "unknown", "conversation status"),
    ("turn", "status", "unknown", "turn status"),
    ("span", "kind", "unknown", "span kind"),
    ("span", "status", "unknown", "span status"),
    ("handoff", "status", "unknown", "handoff status"),
    ("link", "relationship", "unknown", "relationship"),
])
def test_loaded_enum_values_are_rejected(model, field, value, message):
    from blackbox.domain.trace import AgentHandoff, SpanLink
    factories = {
        "conversation": lambda: Conversation.from_dict({field: value}),
        "turn": lambda: Turn.from_dict({
            "conversation_id": "conversation", "agent_id": "agent", field: value
        }),
        "span": lambda: Span.from_dict({
            "name": "span", "trace_id": "trace", "conversation_id": "conversation",
            "turn_id": "turn", field: value,
        }),
        "handoff": lambda: AgentHandoff.from_dict({
            "source_span_id": "source", "target_span_id": "target",
            "source_agent_id": "source-agent", "target_agent_id": "target-agent",
            field: value,
        }),
        "link": lambda: SpanLink.from_dict({
            "trace_id": "trace", "span_id": "span", field: value,
        }),
    }
    with pytest.raises(TraceValidationError, match=message):
        factories[model]()


def test_schema_validates_rich_trace_and_golden_fixture():
    from pathlib import Path
    root = Path(__file__).parents[1]
    schema = json.loads((root / "schemas" / "trace-2.0.schema.json").read_text())
    fixture = json.loads(
        (root / "tests" / "fixtures" / "schemas" / "trace-2.0.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(fixture)

    recorder = Recorder("run", "case", "agent", "question")
    with recorder.conversation():
        with recorder.turn():
            with recorder.span("model", kind="model"):
                recorder.event(ModelRequestEvent("request", {"temperature": 0}))
    jsonschema.Draft202012Validator(schema).validate(recorder.finish().to_dict())


def test_schema_v1_migration_remains_loadable_with_new_contracts():
    legacy = {
        "run_id": "run", "case_id": "case", "agent": "agent",
        "question": "question", "tokens_in": 2, "tokens_out": 3,
    }
    restored = Trace.from_dict(legacy)
    assert restored.total_tokens == 5
    assert restored.usage.total_tokens == 5
    assert restored.conversations == []

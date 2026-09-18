"""Offline, mocked coverage for Phase 5 execution and ingestion."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from types import SimpleNamespace

import jsonschema
import pytest

from blackbox.adapters import (
    APIKeySecret,
    AdapterResponse,
    ConversationHandle,
    DeclarativeMapping,
    FoundryAgentAdapter,
    FoundryTraceImporter,
    HttpAgentAdapter,
    HttpRetryPolicy,
    JSONLTraceImporter,
    OIDCTokenSecret,
    PermanentAdapterError,
    PreparedCase,
    TransientAdapterError,
    WebhookTraceImporter,
    redact,
)
from blackbox.domain.policy import PolicySet
from blackbox.domain.result import RunScore
from blackbox.domain.suite import EvaluationCase
from blackbox.domain.trace import Message, Trace, load_traces
from blackbox.otel import OTLPImportError, OTLPTraceImporter
from blackbox.runtime import AsyncRunner, RunnerConfig, rescore_traces
from blackbox.runtime.stability import run_stability_trials
from blackbox.adapters.http import _PinnedNetworkBackend


def _case(identifier: str, timeout: float | None = None) -> EvaluationCase:
    return EvaluationCase(
        identifier, (Message("user", identifier),), timeout_seconds=timeout
    )


class MockAdapter:
    name = "mock"
    api_version = "2.0"
    supports_seed = True

    def __init__(self, behavior=None):
        self.behavior = behavior or {}
        self.active = self.peak = 0
        self.attempts = {}
        self.closed = False

    async def prepare(self, case: PreparedCase):
        self.attempts[case.case_id] = self.attempts.get(case.case_id, 0) + 1
        if case.case_id == "retry" and self.attempts[case.case_id] == 1:
            raise TransientAdapterError("TEMPORARY", "retry me")
        if case.case_id == "permanent":
            raise PermanentAdapterError("BAD", "do not retry")
        return case

    async def start_conversation(self, prepared, *, correlation_id):
        self.active += 1
        self.peak = max(self.peak, self.active)
        return ConversationHandle(
            f"conversation-{prepared.case_id}",
            {"prepared": prepared},
            {"correlation_id": correlation_id},
        )

    async def send(self, conversation, message):
        if message.content == "slow":
            await asyncio.sleep(0.1)
        else:
            await asyncio.sleep(0.005)
        return AdapterResponse(
            f"answer:{message.content}",
            conversation_id=conversation.conversation_id,
        )

    async def finish_conversation(self, conversation):
        self.active -= 1
        prepared = conversation.state["prepared"]
        return AdapterResponse(
            f"answer:{prepared.case_id}",
            conversation_id=conversation.conversation_id,
            metadata={"seed": prepared.random_seed},
        )

    async def close(self):
        self.closed = True


def test_async_runner_is_bounded_retries_only_typed_transient_and_persists_order(tmp_path):
    adapter = MockAdapter()
    cases = [_case("z"), _case("retry"), _case("permanent"), _case("a")]
    output = tmp_path / "runtime.jsonl"
    run = asyncio.run(AsyncRunner(adapter, RunnerConfig(
        concurrency=2, timeout_seconds=1, max_retries=2,
        retry_delay_seconds=0, random_seed=42,
    )).run(cases, run_id="runtime", output_path=output))

    assert adapter.peak <= 2 and adapter.closed
    assert adapter.attempts["retry"] == 2
    assert adapter.attempts["permanent"] == 1
    assert run.status == "partial"
    assert [trace.case_id for trace in load_traces(output)] == [
        "z", "retry", "permanent", "a"
    ]
    failed = next(item for item in run.cases if item.case_id == "permanent")
    assert failed.error["code"] == "BAD"
    assert all(
        "agentblackbox.random_seed" in item.trace.config
        for item in run.cases if item.status == "completed"
    )


def test_runner_times_out_and_cancellation_is_explicit():
    run = asyncio.run(AsyncRunner(
        MockAdapter(), RunnerConfig(timeout_seconds=0.01)
    ).run([_case("slow")], run_id="timeout"))
    assert run.status == "failed"
    assert run.cases[0].status == "timed_out"
    assert run.cases[0].error["code"] == "CASE_TIMEOUT"


def test_runner_deadline_includes_all_attempts_and_retry_backoff():
    class AlwaysTransient(MockAdapter):
        async def prepare(self, case):
            self.attempts[case.case_id] = self.attempts.get(case.case_id, 0) + 1
            raise TransientAdapterError("BUSY", "retry")

    adapter = AlwaysTransient()
    run = asyncio.run(AsyncRunner(adapter, RunnerConfig(
        timeout_seconds=0.02, max_retries=5, retry_delay_seconds=0.05,
    )).run([_case("deadline")], run_id="deadline"))
    assert run.cases[0].status == "timed_out"
    assert run.cases[0].error["code"] == "CASE_TIMEOUT"
    assert adapter.attempts["deadline"] == 1


def test_http_adapter_streams_maps_auth_retries_and_redacts():
    requests = []
    log_output = io.StringIO()
    logger = logging.getLogger("phase5.http.secrets")
    logger.handlers = [logging.StreamHandler(log_output)]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    async def transport(request):
        requests.append(request)
        if len(requests) == 1:
            raise TransientAdapterError("BUSY", "busy")
        return {
            "status_code": 200,
            "headers": {"content-type": "text/event-stream"},
            "chunks": [b'data: {"content":"hel"}\n', b'data: {"content":"lo"}\n'],
        }

    adapter = HttpAgentAdapter(
        "http://127.0.0.1/mock",
        api_key=APIKeySecret("never-log"),
        oidc_token=lambda: OIDCTokenSecret("also-never-log"),
        allow_http=True,
        allow_private_networks=True,
        streaming=True,
        transport=transport,
        logger=logger,
        retry_policy=HttpRetryPolicy(1, 0, 0),
    )
    prepared = PreparedCase("case", "run", (Message("user", "hello"),))

    async def execute():
        state = await adapter.prepare(prepared)
        conversation = await adapter.start_conversation(state, correlation_id="cid")
        response = await adapter.send(conversation, prepared.messages[0])
        await adapter.close()
        return response

    response = asyncio.run(execute())
    assert response.content == "hello"
    assert len(requests) == 2
    assert requests[-1]["headers"]["authorization"] == "Bearer also-never-log"
    assert requests[-1]["headers"]["api-key"] == "never-log"
    assert requests[-1]["headers"]["x-correlation-id"] == "cid"
    assert "never-log" not in repr(APIKeySecret("never-log"))
    assert redact(requests[-1])["headers"]["authorization"] == "[REDACTED]"
    assert "never-log" not in log_output.getvalue()
    assert "also-never-log" not in log_output.getvalue()


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_http_adapter_blocks_unsafe_destinations_and_redirect_statuses(status):
    with pytest.raises(ValueError, match="HTTPS"):
        HttpAgentAdapter("http://example.com")

    async def redirect(_request):
        return {"status_code": status, "headers": {"location": "https://evil.example"}}

    adapter = HttpAgentAdapter(
        "https://example.com", allow_private_networks=True, transport=redirect
    )

    async def execute():
        case = PreparedCase("case", "run", (Message("user", "x"),))
        conversation = await adapter.start_conversation(case, correlation_id="cid")
        await adapter.send(conversation, case.messages[0])

    with pytest.raises(PermanentAdapterError) as failure:
        asyncio.run(execute())
    assert failure.value.code == "REDIRECT_BLOCKED"


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "169.254.169.254", "fc00::1"])
def test_http_adapter_rejects_unsafe_ipv4_and_ipv6(address):
    adapter = HttpAgentAdapter(
        "https://agent.example",
        resolver=lambda _host, _port: [address],
        transport=lambda _request: {"content": "unused"},
    )
    case = PreparedCase("case", "run", (Message("user", "x"),))
    with pytest.raises(PermanentAdapterError) as failure:
        asyncio.run(adapter.prepare(case))
    assert failure.value.code == "SSRF_BLOCKED"


def test_http_adapter_pins_validated_ipv4_ipv6_once_and_keeps_origin():
    requests, resolutions = [], []

    async def resolver(host, port):
        resolutions.append((host, port))
        return ["8.8.8.8", "2606:4700:4700::1111"]

    async def transport(request):
        requests.append(request)
        return {"content": "ok"}

    adapter = HttpAgentAdapter(
        "https://agent.example/v1",
        resolver=resolver,
        transport=transport,
    )
    prepared = PreparedCase("case", "run", (Message("user", "hello"),))

    async def execute():
        await adapter.prepare(prepared)
        await adapter.prepare(prepared)
        conversation = await adapter.start_conversation(
            prepared, correlation_id="cid"
        )
        return await adapter.send(conversation, prepared.messages[0])

    assert asyncio.run(execute()).content == "ok"
    assert resolutions == [("agent.example", 443)]
    assert requests[0]["url"] == "https://agent.example/v1"
    assert requests[0]["resolved_addresses"] == (
        "8.8.8.8", "2606:4700:4700::1111"
    )


def test_pinned_backend_dials_only_validated_addresses():
    class Delegate:
        def __init__(self):
            self.hosts = []

        async def connect_tcp(self, host, *_args):
            self.hosts.append(host)
            if host == "8.8.8.8":
                raise OSError("try IPv6")
            return "stream"

    delegate = Delegate()
    backend = _PinnedNetworkBackend(
        "agent.example", ("8.8.8.8", "2606:4700:4700::1111"), delegate
    )
    assert asyncio.run(backend.connect_tcp("agent.example", 443)) == "stream"
    assert delegate.hosts == ["8.8.8.8", "2606:4700:4700::1111"]
    with pytest.raises(PermanentAdapterError, match="unvalidated host"):
        asyncio.run(backend.connect_tcp("rebound.example", 443))


def test_foundry_import_preserves_w3c_ids_and_namespaced_metadata():
    document = {
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "thread_id": "foundry-thread",
        "run_id": "foundry-run",
        "agent_id": "agent-1",
        "region": "eastus",
        "messages": [
            {"id": "m1", "role": "user", "content": "hello"},
            {"id": "m2", "role": "assistant", "content": "world"},
        ],
        "spans": [{
            "id": "00f067aa0ba902b7",
            "name": "agent",
            "kind": "agent",
            "attributes": {"model": "gpt-mock"},
        }],
    }
    trace = FoundryTraceImporter().import_trace(document)
    assert trace.trace_id == document["trace_id"]
    assert trace.conversation_id == "foundry-thread"
    assert trace.spans[0].span_id == "00f067aa0ba902b7"
    assert trace.spans[0].attributes["foundry.model"] == "gpt-mock"
    assert trace.config["foundry.metadata"]["foundry.region"] == "eastus"


def test_foundry_adapter_lifecycle_supports_strict_sdk_signatures():
    calls = []
    attempts = 0

    async def create_thread(*, metadata):
        calls.append(("thread", metadata))
        return {"id": "thread-1"}

    async def create_message(*, thread_id, role, content):
        calls.append(("message", thread_id, role, content))
        return {"content": "fallback"}

    async def create_run(*, thread_id, agent_id):
        nonlocal attempts
        attempts += 1
        calls.append(("run", thread_id, agent_id))
        if attempts == 1:
            error = RuntimeError("busy")
            error.status_code = 503
            raise error
        return {"id": "run-1", "status": "completed", "output": "answer"}

    client = SimpleNamespace(agents=SimpleNamespace(
        threads=SimpleNamespace(create=create_thread),
        messages=SimpleNamespace(create=create_message),
        runs=SimpleNamespace(create_and_process=create_run),
    ))
    adapter = FoundryAgentAdapter(
        "https://example.services.ai.azure.com/api/projects/test",
        "agent-1",
        client=client,
    )
    prepared = PreparedCase("case", "run", (Message("user", "hello"),))

    async def execute():
        state = await adapter.prepare(prepared)
        conversation = await adapter.start_conversation(state, correlation_id="cid")
        response = await adapter.send(conversation, prepared.messages[0])
        assert await adapter.finish_conversation(conversation) is response
        await adapter.close()
        return response

    response = asyncio.run(execute())
    assert response.content == "answer"
    assert calls[-1] == ("run", "thread-1", "agent-1")
    assert [call[0] for call in calls].count("message") == 1
    assert [call[0] for call in calls].count("run") == 2


def test_foundry_exhausted_run_retries_never_resubmit_prompt():
    counts = {"messages": 0, "runs": 0}

    async def create_thread(*, metadata):
        return {"id": "thread-1"}

    async def create_message(*, thread_id, role, content):
        counts["messages"] += 1
        return {"content": ""}

    async def create_run(*, thread_id, agent_id):
        counts["runs"] += 1
        error = RuntimeError("busy")
        error.status_code = 503
        raise error

    client = SimpleNamespace(agents=SimpleNamespace(
        threads=SimpleNamespace(create=create_thread),
        messages=SimpleNamespace(create=create_message),
        runs=SimpleNamespace(create_and_process=create_run),
    ))
    adapter = FoundryAgentAdapter(
        "https://example.services.ai.azure.com/api/projects/test",
        "agent-1", client=client, max_retries=1,
    )
    run = asyncio.run(AsyncRunner(
        adapter, RunnerConfig(max_retries=3, retry_delay_seconds=0)
    ).run([_case("case")], run_id="foundry"))
    assert run.cases[0].status == "failed"
    assert run.cases[0].attempts == 1
    assert run.cases[0].error["code"] == "FOUNDRY_RUN_RETRIES_EXHAUSTED"
    assert counts == {"messages": 1, "runs": 2}


def test_otlp_genai_mapping_preserves_unknown_attributes_and_rejects_large_payload():
    document = {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "sample-agent"}}
            ]},
            "scopeSpans": [{"scope": {"name": "test"}, "spans": [{
                "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                "spanId": "00f067aa0ba902b7",
                "name": "chat",
                "startTimeUnixNano": "10",
                "endTimeUnixNano": "20",
                "attributes": [
                    {"key": "gen_ai.operation.name",
                     "value": {"stringValue": "chat"}},
                    {"key": "gen_ai.input.messages",
                     "value": {"stringValue": '[{"role":"user","content":"hi"}]'}},
                    {"key": "gen_ai.output.messages",
                     "value": {"stringValue": '[{"role":"assistant","content":"hello"}]'}},
                    {"key": "vendor.future", "value": {"stringValue": "preserved"}},
                ],
            }]}],
        }]
    }
    traces = OTLPTraceImporter().import_http(json.dumps(document).encode())
    assert traces[0].answer == "hello"
    assert traces[0].spans[0].kind == "model"
    assert traces[0].spans[0].attributes["otel.attributes"]["vendor.future"] == "preserved"
    with pytest.raises(OTLPImportError) as failure:
        OTLPTraceImporter(payload_limit_bytes=2).import_http(b"{}x")
    assert failure.value.code == "OTLP_PAYLOAD_TOO_LARGE"


def test_otlp_finds_input_messages_on_later_ordered_span():
    def span(identifier, start, attributes):
        return {
            "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
            "spanId": identifier,
            "name": "operation",
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(start + 1),
            "attributes": attributes,
        }

    document = {"resourceSpans": [{"scopeSpans": [{"spans": [
        span("0000000000000001", 1, []),
        span("0000000000000002", 2, [{
            "key": "gen_ai.input.messages",
            "value": {"stringValue": '[{"role":"user","content":"later prompt"}]'},
        }]),
        span("0000000000000003", 3, [{
            "key": "gen_ai.output.messages",
            "value": {"stringValue": '[{"role":"assistant","content":"answer"}]'},
        }]),
    ]}]}]}
    trace = OTLPTraceImporter().import_document(document)[0]
    assert trace.question == "later prompt"
    assert trace.answer == "answer"


def test_generic_webhook_and_jsonl_mapping_validate_schema(tmp_path):
    source = Trace("run", "case", "agent", "question", answer="answer").to_dict()
    wrapper = {"payload": source}
    mapping = DeclarativeMapping({
        key: f"payload.{key}" for key in source
    })
    importer = WebhookTraceImporter(mapping)
    assert importer.import_payload(wrapper).answer == "answer"

    path = tmp_path / "mapped.jsonl"
    path.write_text(json.dumps(wrapper) + "\n", encoding="utf-8")
    assert JSONLTraceImporter(mapping).import_file(path)[0].case_id == "case"

    schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"const": "different"}},
    }
    with pytest.raises(PermanentAdapterError) as failure:
        WebhookTraceImporter(mapping, schema=schema).import_payload(wrapper)
    assert failure.value.code == "IMPORT_SCHEMA_INVALID"


def test_rescore_never_invokes_agent_and_does_not_mutate_evidence(tmp_path):
    trace = Trace("stored", "case", "agent", "question", answer="answer")
    case = _case("case")
    original = trace.to_dict()
    result = rescore_traces(
        [trace], [case], policies=PolicySet(require_citations=False),
        run_id="rescored", output_directory=tmp_path,
    )
    assert result.score.run_id == "rescored"
    assert result.output_path.exists()
    assert trace.to_dict() == original


def test_repeated_trials_report_exact_semantic_intervals_and_seeds():
    report = asyncio.run(run_stability_trials(
        MockAdapter, [_case("stable")], trials=3, seed=17,
        semantic_threshold=0.5,
        runner_config=RunnerConfig(retry_delay_seconds=0),
    ))
    assert report.exact.value == report.semantic.value == 1
    assert report.exact.comparisons == 3
    assert report.exact.confidence_interval[1] == 1
    assert report.seed == 17
    assert len(report.case_seeds) == 3


def test_stability_trials_represent_failures_without_seed_key_errors():
    report = asyncio.run(run_stability_trials(
        MockAdapter, [_case("permanent"), _case("slow")], trials=3, seed=17,
        runner_config=RunnerConfig(
            timeout_seconds=0.01, retry_delay_seconds=0
        ),
    ))
    assert report.exact.value == report.semantic.value == 1
    assert report.exact.comparisons == 6
    assert len(report.case_seeds) == 6
    assert report.case_outcomes["permanent"] == (
        "failed:BAD", "failed:BAD", "failed:BAD"
    )
    assert report.case_outcomes["slow"] == (
        "timed_out:CASE_TIMEOUT",
        "timed_out:CASE_TIMEOUT",
        "timed_out:CASE_TIMEOUT",
    )


def test_import_schema_is_valid_and_phase_tasks_are_marked_complete():
    for name in ("import-2.0.schema.json", "result-2.0.schema.json"):
        schema = json.loads(open(f"schemas/{name}", encoding="utf-8").read())
        jsonschema.Draft202012Validator.check_schema(schema)
    result_schema = json.loads(
        open("schemas/result-2.0.schema.json", encoding="utf-8").read()
    )
    score = RunScore("run", "agent", [], 1.0)
    score.stability = {
        "trials": 3, "comparisons": 3,
        "exact_agreement": 1.0,
        "exact_confidence_interval": [0.44, 1.0],
        "semantic_agreement": 1.0,
        "semantic_confidence_interval": [0.44, 1.0],
        "semantic_threshold": 0.8,
        "random_seed": None, "trial_seeds": [],
    }
    jsonschema.validate(score.to_dict(), result_schema)
    plan = open(
        "plan/architecture-production-platform-1.md", encoding="utf-8"
    ).read()
    for task in range(29, 37):
        line = next(value for value in plan.splitlines()
                    if f"TASK-{task:03d}" in value)
        assert "| Yes | 2026-09-18 |" in line

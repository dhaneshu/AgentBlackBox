"""OTLP HTTP trace decoding and OpenTelemetry GenAI semantic mapping."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from blackbox.domain.trace import (
    Conversation,
    CustomEvent,
    Message,
    Span,
    SpanLink,
    Trace,
    Turn,
    Usage,
    new_ulid,
)
from blackbox.adapters.http import redact


class OTLPImportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class OTLPTraceImporter:
    def __init__(self, *, payload_limit_bytes: int = 4_194_304) -> None:
        if payload_limit_bytes <= 0:
            raise ValueError("payload_limit_bytes must be positive")
        self.payload_limit_bytes = payload_limit_bytes

    def import_http(
        self,
        body: bytes,
        *,
        content_type: str = "application/json",
        content_encoding: str = "identity",
    ) -> list[Trace]:
        if len(body) > self.payload_limit_bytes:
            raise OTLPImportError(
                "OTLP_PAYLOAD_TOO_LARGE",
                f"OTLP payload is {len(body)} bytes; limit is {self.payload_limit_bytes}",
            )
        if content_encoding.lower() not in {"", "identity"}:
            raise OTLPImportError(
                "OTLP_UNSUPPORTED_ENCODING",
                "compressed OTLP payloads are not accepted by the local importer",
            )
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type in {"application/json", "application/x-json"}:
            try:
                document = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise OTLPImportError(
                    "OTLP_INVALID_JSON", "OTLP body is not valid UTF-8 JSON"
                ) from exc
        elif media_type in {"application/x-protobuf", "application/protobuf"}:
            document = self._decode_protobuf(body)
        else:
            raise OTLPImportError(
                "OTLP_UNSUPPORTED_CONTENT_TYPE",
                f"unsupported OTLP content type {content_type!r}",
            )
        return self.import_document(document)

    def import_document(self, document: Mapping[str, Any]) -> list[Trace]:
        if not isinstance(document, Mapping):
            raise OTLPImportError("OTLP_INVALID_DOCUMENT", "OTLP export must be an object")
        grouped: dict[str, list[tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]]]] = (
            defaultdict(list)
        )
        for resource_spans in _items(document, "resourceSpans", "resource_spans"):
            resource = _attributes(_field(resource_spans, "resource", {}))
            scopes = _items(resource_spans, "scopeSpans", "scope_spans")
            for scope_spans in scopes:
                scope = dict(_field(scope_spans, "scope", {}) or {})
                for raw_span in _items(scope_spans, "spans"):
                    if not isinstance(raw_span, Mapping):
                        raise OTLPImportError(
                            "OTLP_INVALID_SPAN", "OTLP span entries must be objects"
                        )
                    trace_id = _id(_field(raw_span, "traceId", _field(raw_span, "trace_id", "")))
                    if not trace_id:
                        raise OTLPImportError(
                            "OTLP_MISSING_TRACE_ID", "OTLP span is missing traceId"
                        )
                    grouped[trace_id].append((raw_span, resource, scope))
        if not grouped:
            return []
        return [
            self._map_trace(trace_id, values)
            for trace_id, values in sorted(grouped.items())
        ]

    def _map_trace(
        self,
        trace_id: str,
        values: list[tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> Trace:
        combined = [(item, _attributes(item), resource, scope)
                    for item, resource, scope in values]
        combined.sort(key=lambda value: (
            int(_field(value[0], "startTimeUnixNano",
                       _field(value[0], "start_time_unix_nano", 0))),
            _id(_field(value[0], "spanId", _field(value[0], "span_id", ""))),
        ))
        first_attrs = combined[0][1]
        conversation_id = str(
            _semantic(first_attrs, "gen_ai.conversation.id", "gen_ai.thread.id")
            or f"otel-{trace_id}"
        )
        turn_id = str(
            _semantic(first_attrs, "gen_ai.request.id", "gen_ai.response.id")
            or f"turn-{trace_id}"
        )
        agent = str(
            _semantic(first_attrs, "gen_ai.agent.name", "gen_ai.agent.id")
            or "otel-agent"
        )
        known_span_ids = {
            _id(_field(item, "spanId", _field(item, "span_id", "")))
            for item, _, _, _ in combined
        }
        spans = [
            self._map_span(
                item, attributes, resource, scope, trace_id,
                conversation_id, turn_id, agent, known_span_ids,
            )
            for item, attributes, resource, scope in combined
        ]
        input_messages: list[Message] = []
        for _, attrs, _, _ in combined:
            input_messages = _messages(
                _semantic(attrs, "gen_ai.input.messages", "gen_ai.prompt")
            )
            if input_messages:
                break
        output_messages: list[Message] = []
        for _, attrs, _, _ in reversed(combined):
            output_messages = _messages(
                _semantic(attrs, "gen_ai.output.messages", "gen_ai.completion"),
                default_role="assistant",
            )
            if output_messages:
                break
        messages = [*input_messages, *output_messages]
        if not messages:
            messages = [Message("user", "")]
        question = next(
            (str(message.content) for message in reversed(messages)
             if message.role == "user"), ""
        )
        answer = next(
            (str(message.content) for message in reversed(messages)
             if message.role == "assistant"), ""
        )
        usage = Usage(
            sum(int(_semantic(attrs, "gen_ai.usage.input_tokens") or 0)
                for _, attrs, _, _ in combined),
            sum(int(_semantic(attrs, "gen_ai.usage.output_tokens") or 0)
                for _, attrs, _, _ in combined),
            sum(int(_semantic(attrs, "gen_ai.usage.cached_input_tokens") or 0)
                for _, attrs, _, _ in combined),
            sum(1 for span in spans if span.kind == "tool"),
        )
        turn = Turn(
            conversation_id, agent, turn_id, messages,
            [span.span_id for span in spans], "completed",
        )
        conversation = Conversation(
            conversation_id, "Imported OTLP trace", [agent], [turn], "completed"
        )
        root_resource = combined[0][2]
        run_id = str(
            root_resource.get("service.instance.id")
            or root_resource.get("service.name")
            or f"otel-{trace_id[:16]}"
        )
        trace = Trace(
            run_id, str(first_attrs.get("agentblackbox.case_id", trace_id)),
            agent, question, answer=answer, trace_id=trace_id,
            conversation_id=conversation_id, turn_id=turn_id,
            conversations=[conversation], spans=spans, usage=usage,
            tokens_in=usage.input_tokens, tokens_out=usage.output_tokens,
            payload_limit_bytes=self.payload_limit_bytes,
            config={
                "otel.resource": root_resource,
                "otel.scope": combined[0][3],
                "otel.imported": True,
            },
        )
        trace.validate()
        return trace

    def _map_span(
        self,
        raw: Mapping[str, Any],
        attributes: dict[str, Any],
        resource: dict[str, Any],
        scope: dict[str, Any],
        trace_id: str,
        conversation_id: str,
        turn_id: str,
        agent: str,
        known_span_ids: set[str],
    ) -> Span:
        span_id = _id(_field(raw, "spanId", _field(raw, "span_id", ""))) or new_ulid()
        parent_id = _id(_field(raw, "parentSpanId", _field(raw, "parent_span_id", "")))
        preserved = {
            "otel.attributes": attributes,
            "otel.resource": resource,
            "otel.scope": scope,
            "otel.kind": _field(raw, "kind", ""),
        }
        if parent_id and parent_id not in known_span_ids:
            preserved["otel.orphan_parent_span_id"] = parent_id
            parent_id = ""
        operation = str(
            _semantic(attributes, "gen_ai.operation.name")
            or _field(raw, "name", "otel.operation")
        )
        kind = _span_kind(operation, attributes)
        start = int(_field(raw, "startTimeUnixNano",
                           _field(raw, "start_time_unix_nano", 0)))
        end = int(_field(raw, "endTimeUnixNano",
                         _field(raw, "end_time_unix_nano", start)))
        status_data = _field(raw, "status", {}) or {}
        status_code = str(_field(status_data, "code", "STATUS_CODE_UNSET")).upper()
        status = "error" if status_code in {"2", "STATUS_CODE_ERROR", "ERROR"} else "ok"
        events = [
            CustomEvent(
                str(_field(event, "name", "otel.event")),
                {
                    "otel.time_unix_nano": int(_field(
                        event, "timeUnixNano", _field(event, "time_unix_nano", 0)
                    )),
                    "otel.attributes": _attributes(event),
                },
            )
            for event in _items(raw, "events")
        ]
        links = [
            SpanLink(
                _id(_field(link, "traceId", _field(link, "trace_id", ""))),
                _id(_field(link, "spanId", _field(link, "span_id", ""))),
                "related",
                {"otel.attributes": _attributes(link)},
            )
            for link in _items(raw, "links")
        ]
        usage = Usage(
            int(_semantic(attributes, "gen_ai.usage.input_tokens") or 0),
            int(_semantic(attributes, "gen_ai.usage.output_tokens") or 0),
            int(_semantic(attributes, "gen_ai.usage.cached_input_tokens") or 0),
            1 if kind == "tool" else 0,
        )
        return Span(
            str(_field(raw, "name", operation)), trace_id, conversation_id, turn_id,
            span_id, parent_id or None, kind, agent, status,
            start_ns=start, end_ns=end, duration_ns=max(0, end - start),
            links=links, events=events, attributes=preserved, usage=usage,
        )

    def _decode_protobuf(self, body: bytes) -> Mapping[str, Any]:
        try:
            from google.protobuf.json_format import MessageToDict
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                ExportTraceServiceRequest,
            )
        except ImportError as exc:
            raise OTLPImportError(
                "OTLP_PROTOBUF_UNAVAILABLE",
                "protobuf OTLP requires the optional 'otel' dependency",
            ) from exc
        request = ExportTraceServiceRequest()
        try:
            request.ParseFromString(body)
        except Exception as exc:
            raise OTLPImportError(
                "OTLP_INVALID_PROTOBUF", "OTLP protobuf body cannot be decoded"
            ) from exc
        return MessageToDict(request, preserving_proto_field_name=False)


def _items(value: Mapping[str, Any], *names: str) -> list[Any]:
    for name in names:
        found = value.get(name)
        if found is not None:
            if not isinstance(found, list):
                raise OTLPImportError("OTLP_INVALID_DOCUMENT", f"{name} must be an array")
            return found
    return []


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else default


def _id(value: Any) -> str:
    if isinstance(value, bytes):
        return value.hex()
    return str(value or "").lower()


def _attributes(container: Any) -> dict[str, Any]:
    raw = _field(container, "attributes", [])
    if isinstance(raw, Mapping):
        return redact(dict(raw))
    result: dict[str, Any] = {}
    for item in raw or ():
        if not isinstance(item, Mapping) or "key" not in item:
            continue
        result[str(item["key"])] = _any_value(item.get("value"))
    return redact(result)


def _any_value(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    names = (
        "stringValue", "boolValue", "intValue", "doubleValue", "bytesValue",
        "string_value", "bool_value", "int_value", "double_value", "bytes_value",
    )
    for name in names:
        if name in value:
            raw = value[name]
            if "int" in name:
                return int(raw)
            return raw
    array = value.get("arrayValue", value.get("array_value"))
    if isinstance(array, Mapping):
        return [_any_value(item) for item in array.get("values", ())]
    kv = value.get("kvlistValue", value.get("kvlist_value"))
    if isinstance(kv, Mapping):
        return {
            str(item["key"]): _any_value(item.get("value"))
            for item in kv.get("values", ()) if isinstance(item, Mapping)
        }
    return dict(value)


def _semantic(attributes: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in attributes:
            return attributes[name]
    return None


def _messages(value: Any, *, default_role: str = "user") -> list[Message]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return [Message(default_role, value)]
        return _messages(decoded, default_role=default_role)
    if isinstance(value, Mapping):
        role = str(value.get("role", default_role))
        content = value.get("content", value.get("text", ""))
        if isinstance(content, list):
            text = "".join(
                str(item.get("text", item)) if isinstance(item, Mapping) else str(item)
                for item in content
            )
            content = text
        return [Message(role if role in {
            "system", "user", "assistant", "tool", "developer"
        } else default_role, content)]
    if isinstance(value, list):
        return [
            message for item in value
            for message in _messages(item, default_role=default_role)
        ]
    return [Message(default_role, str(value))]


def _span_kind(operation: str, attributes: Mapping[str, Any]) -> str:
    lowered = operation.lower()
    if "tool" in lowered or "gen_ai.tool.name" in attributes:
        return "tool"
    if any(word in lowered for word in ("chat", "completion", "embedding", "model")):
        return "model"
    if any(word in lowered for word in ("retrieve", "search", "rerank")):
        return "retrieval"
    if "agent" in lowered or any(key.startswith("gen_ai.agent.") for key in attributes):
        return "agent"
    return "internal"

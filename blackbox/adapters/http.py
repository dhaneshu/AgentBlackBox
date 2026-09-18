"""Hardened HTTP adapter with declarative request and response mappings."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from blackbox.domain.trace import Message, Trace, canonical_json, new_ulid

from .base import (
    API_VERSION,
    AdapterResponse,
    ConversationHandle,
    PermanentAdapterError,
    PreparedCase,
    TransientAdapterError,
    maybe_await,
)

DEFAULT_HTTP_PAYLOAD_LIMIT = 1_048_576
_SENSITIVE = frozenset({
    "authorization", "api-key", "api_key", "apikey", "access_token",
    "refresh_token", "password", "secret", "token", "cookie", "set-cookie",
})


class SecretValue:
    """Opaque secret whose repr and string conversion never disclose its value."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("secret cannot be empty")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"{type(self).__name__}('[REDACTED]')"

    def __str__(self) -> str:
        return "[REDACTED]"


class APIKeySecret(SecretValue):
    pass


class OIDCTokenSecret(SecretValue):
    pass


@dataclass(frozen=True)
class HttpRetryPolicy:
    max_retries: int = 2
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")

    def delay(self, retry: int) -> float:
        return min(self.max_delay_seconds, self.base_delay_seconds * (2 ** retry))


Transport = Callable[[dict[str, Any]], Any | Awaitable[Any]]
Resolver = Callable[[str, int], Any | Awaitable[Any]]


class HttpAgentAdapter:
    """Call an HTTP agent without permitting redirects or private-network SSRF."""

    api_version = API_VERSION

    def __init__(
        self,
        url: str,
        *,
        name: str = "http-agent",
        method: str = "POST",
        request_mapping: Mapping[str, str] | None = None,
        response_mapping: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        api_key: APIKeySecret | None = None,
        api_key_header: str = "api-key",
        oidc_token: OIDCTokenSecret | Callable[[], Any] | None = None,
        retry_policy: HttpRetryPolicy | None = None,
        timeout_seconds: float = 30.0,
        payload_limit_bytes: int = DEFAULT_HTTP_PAYLOAD_LIMIT,
        allowed_hosts: tuple[str, ...] = (),
        allow_http: bool = False,
        allow_private_networks: bool = False,
        streaming: bool = False,
        transport: Transport | None = None,
        resolver: Resolver | None = None,
        logger: logging.Logger | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_seed: int | None = None,
    ) -> None:
        if timeout_seconds <= 0 or payload_limit_bytes <= 0:
            raise ValueError("timeout and payload limit must be positive")
        self.url, self.name, self.method = url, name, method.upper()
        self.request_mapping = dict(request_mapping or {
            "message": "message.content",
            "conversation_id": "conversation.conversation_id",
            "correlation_id": "conversation.metadata.correlation_id",
        })
        self.response_mapping = dict(response_mapping or {
            "content": "content",
            "refused": "refused",
            "refusal_reason": "refusal_reason",
            "conversation_id": "conversation_id",
            "trace_id": "trace_id",
        })
        self.headers = dict(headers or {})
        self.api_key, self.api_key_header, self.oidc_token = (
            api_key, api_key_header, oidc_token
        )
        self.retry_policy = retry_policy or HttpRetryPolicy()
        self.timeout_seconds = timeout_seconds
        self.payload_limit_bytes = payload_limit_bytes
        self.allowed_hosts = tuple(value.lower() for value in allowed_hosts)
        self.allow_http = allow_http
        self.allow_private_networks = allow_private_networks
        self.streaming = streaming
        self.transport = transport
        self.resolver = resolver
        self.logger = logger or logging.getLogger(__name__)
        self.sleeper = sleeper
        self.random_seed = random_seed
        self.supports_seed = random_seed is not None
        self._client: Any = None
        self._closed = False
        self._resolved_addresses: tuple[str, ...] | None = None
        self._resolution_lock = asyncio.Lock()
        _validate_url_shape(url, self.allowed_hosts, allow_http)

    async def prepare(self, case: PreparedCase) -> PreparedCase:
        if self._closed:
            raise PermanentAdapterError("ADAPTER_CLOSED", "adapter is closed")
        await self._validate_destination()
        return case

    async def start_conversation(
        self, prepared: PreparedCase, *, correlation_id: str
    ) -> ConversationHandle:
        return ConversationHandle(
            new_ulid(),
            {"prepared": prepared, "responses": []},
            {"correlation_id": correlation_id},
        )

    async def send(
        self, conversation: ConversationHandle, message: Message
    ) -> AdapterResponse:
        context = {
            "message": message.to_dict(),
            "conversation": {
                "conversation_id": conversation.conversation_id,
                "metadata": conversation.metadata,
            },
            "case": conversation.state["prepared"],
            "seed": conversation.state["prepared"].random_seed,
        }
        payload = {
            target: _get_path(context, source)
            for target, source in self.request_mapping.items()
        }
        encoded = canonical_json(payload).encode("utf-8")
        if len(encoded) > self.payload_limit_bytes:
            raise PermanentAdapterError(
                "REQUEST_TOO_LARGE",
                f"HTTP request is {len(encoded)} bytes; limit is {self.payload_limit_bytes}",
            )
        headers = await self._request_headers(conversation)
        request = {
            "method": self.method,
            "url": self.url,
            "headers": headers,
            "json": payload,
            "timeout": self.timeout_seconds,
            "follow_redirects": False,
            "stream": self.streaming,
            "resolved_addresses": self._resolved_addresses or (),
        }
        log_headers = dict(headers)
        if self.api_key:
            log_headers[self.api_key_header] = "[REDACTED]"
        self.logger.info("HTTP agent request %s", redact({
            "method": self.method, "url": self.url, "headers": log_headers,
            "payload": payload,
        }))
        result = await self._request_with_retries(request)
        document = await self._decode_response(result)
        response = self._map_response(document, conversation)
        conversation.state["responses"].append(response)
        return response

    async def finish_conversation(
        self, conversation: ConversationHandle
    ) -> AdapterResponse | None:
        responses = conversation.state.get("responses", ())
        return responses[-1] if responses else None

    async def close(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _validate_destination(self) -> None:
        if self._resolved_addresses is not None:
            return
        parsed = urlparse(self.url)
        assert parsed.hostname
        # Explicitly permitted injected transports stay fully offline-testable.
        if self.transport is not None and self.allow_private_networks and not self.resolver:
            self._resolved_addresses = ()
            return
        async with self._resolution_lock:
            if self._resolved_addresses is not None:
                return
            try:
                if self.resolver:
                    records = await maybe_await(
                        self.resolver(parsed.hostname, parsed.port or 443)
                    )
                else:
                    records = await asyncio.get_running_loop().run_in_executor(
                        None, socket.getaddrinfo,
                        parsed.hostname, parsed.port or 443,
                    )
            except (OSError, socket.gaierror) as exc:
                raise PermanentAdapterError(
                    "DESTINATION_UNRESOLVED",
                    f"HTTP adapter destination cannot be resolved: {parsed.hostname}",
                ) from exc
            addresses = tuple(dict.fromkeys(_resolved_ip(record) for record in records))
            if not addresses:
                raise PermanentAdapterError(
                    "DESTINATION_UNRESOLVED",
                    f"HTTP adapter destination cannot be resolved: {parsed.hostname}",
                )
            for value in addresses:
                address = ipaddress.ip_address(value)
                if not self.allow_private_networks and _is_unsafe_address(address):
                    raise PermanentAdapterError(
                        "SSRF_BLOCKED",
                        "HTTP adapter destination resolves to a non-public "
                        f"address: {address}",
                    )
            self._resolved_addresses = addresses

    async def _request_headers(
        self, conversation: ConversationHandle
    ) -> dict[str, str]:
        headers = {**self.headers}
        headers.setdefault("content-type", "application/json")
        headers["x-correlation-id"] = str(conversation.metadata["correlation_id"])
        if self.api_key:
            headers[self.api_key_header] = self.api_key.reveal()
        if self.oidc_token:
            token = (
                await maybe_await(self.oidc_token())
                if callable(self.oidc_token)
                else self.oidc_token
            )
            if not isinstance(token, OIDCTokenSecret):
                raise PermanentAdapterError(
                    "INVALID_AUTH_SECRET",
                    "OIDC token providers must return OIDCTokenSecret",
                )
            headers["authorization"] = f"Bearer {token.reveal()}"
        return headers

    async def _request_with_retries(self, request: dict[str, Any]) -> Any:
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                return await self._request(request)
            except TransientAdapterError:
                if attempt >= self.retry_policy.max_retries:
                    raise
                await self.sleeper(self.retry_policy.delay(attempt))
            except PermanentAdapterError:
                raise
            except Exception as exc:
                raise PermanentAdapterError(
                    "HTTP_TRANSPORT_FAILED",
                    f"HTTP transport failed: {type(exc).__name__}",
                ) from exc
        raise AssertionError("unreachable")

    async def _request(self, request: dict[str, Any]) -> Any:
        if self.transport:
            return await maybe_await(self.transport(request))
        try:
            import httpx
        except ImportError as exc:
            raise PermanentAdapterError(
                "MISSING_HTTP_DEPENDENCY",
                "HttpAgentAdapter requires the optional 'http' dependency",
            ) from exc
        if self._resolved_addresses is None:
            await self._validate_destination()
        if not self._resolved_addresses:
            raise PermanentAdapterError(
                "DESTINATION_UNRESOLVED",
                "HTTP destination has no validated address",
            )
        if self._client is None:
            pinned_transport = _pinned_httpx_transport(
                urlparse(self.url).hostname or "", self._resolved_addresses
            )
            self._client = httpx.AsyncClient(
                follow_redirects=False, timeout=self.timeout_seconds,
                transport=pinned_transport,
            )
        try:
            if self.streaming:
                async with self._client.stream(
                    request["method"], request["url"], headers=request["headers"],
                    json=request["json"],
                ) as response:
                    self._check_status(response.status_code, dict(response.headers))
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.payload_limit_bytes:
                            raise PermanentAdapterError(
                                "RESPONSE_TOO_LARGE",
                                f"HTTP response exceeds {self.payload_limit_bytes} bytes",
                            )
                        chunks.append(chunk)
                    return {"status_code": response.status_code,
                            "headers": dict(response.headers), "chunks": chunks}
            response = await self._client.request(
                request["method"], request["url"], headers=request["headers"],
                json=request["json"],
            )
            self._check_status(response.status_code, dict(response.headers))
            return {"status_code": response.status_code,
                    "headers": dict(response.headers), "content": response.content}
        except (httpx.TimeoutException, httpx.NetworkError,
                httpx.RemoteProtocolError) as exc:
            raise TransientAdapterError(
                "HTTP_TRANSIENT_TRANSPORT",
                f"HTTP transport is temporarily unavailable: {type(exc).__name__}",
            ) from exc

    def _check_status(self, status: int, headers: Mapping[str, str]) -> None:
        if 300 <= status < 400:
            raise PermanentAdapterError(
                "REDIRECT_BLOCKED", "HTTP agent redirects are not permitted"
            )
        if status in {408, 425, 429} or status >= 500:
            raise TransientAdapterError(
                "HTTP_TRANSIENT_STATUS", f"HTTP agent returned {status}",
                details={"status": status},
            )
        if status >= 400:
            raise PermanentAdapterError(
                "HTTP_PERMANENT_STATUS", f"HTTP agent returned {status}",
                details={"status": status},
            )

    async def _decode_response(self, result: Any) -> Any:
        if isinstance(result, AdapterResponse):
            return result
        status = int(_field(result, "status_code", 200))
        headers = dict(_field(result, "headers", {}) or {})
        self._check_status(status, headers)
        if _field(result, "url", self.url) != self.url:
            raise PermanentAdapterError(
                "REDIRECT_BLOCKED", "HTTP transport returned a redirected URL"
            )
        if (
            isinstance(result, Mapping)
            and not any(key in result for key in ("status_code", "headers", "chunks"))
        ):
            document = result
        elif _field(result, "chunks", None) is not None:
            chunks = _field(result, "chunks", ())
            values: list[bytes] = []
            size = 0
            if isinstance(chunks, AsyncIterable):
                async for chunk in chunks:
                    encoded = chunk if isinstance(chunk, bytes) else str(chunk).encode()
                    size += len(encoded)
                    if size > self.payload_limit_bytes:
                        raise PermanentAdapterError(
                            "RESPONSE_TOO_LARGE",
                            f"HTTP response exceeds {self.payload_limit_bytes} bytes",
                        )
                    values.append(encoded)
            else:
                for chunk in chunks:
                    encoded = chunk if isinstance(chunk, bytes) else str(chunk).encode()
                    size += len(encoded)
                    if size > self.payload_limit_bytes:
                        raise PermanentAdapterError(
                            "RESPONSE_TOO_LARGE",
                            f"HTTP response exceeds {self.payload_limit_bytes} bytes",
                        )
                    values.append(encoded)
            raw = b"".join(values)
            document = _decode_stream(raw)
        else:
            content = _field(result, "content", result)
            if hasattr(result, "json") and callable(result.json):
                try:
                    document = result.json()
                except (ValueError, UnicodeDecodeError) as exc:
                    raise PermanentAdapterError(
                        "INVALID_RESPONSE", "HTTP agent response is not valid JSON"
                    ) from exc
            elif isinstance(content, (dict, list)):
                document = content
            else:
                raw = content if isinstance(content, bytes) else str(content).encode()
                if len(raw) > self.payload_limit_bytes:
                    raise PermanentAdapterError(
                        "RESPONSE_TOO_LARGE",
                        f"HTTP response exceeds {self.payload_limit_bytes} bytes",
                    )
                try:
                    document = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise PermanentAdapterError(
                        "INVALID_RESPONSE", "HTTP agent response is not valid JSON"
                    ) from exc
        size = len(canonical_json(document).encode())
        if size > self.payload_limit_bytes:
            raise PermanentAdapterError(
                "RESPONSE_TOO_LARGE",
                f"HTTP response is {size} bytes; limit is {self.payload_limit_bytes}",
            )
        return document

    def _map_response(
        self, document: Any, conversation: ConversationHandle
    ) -> AdapterResponse:
        if isinstance(document, AdapterResponse):
            return document
        mapped: dict[str, Any] = {}
        for target, source in self.response_mapping.items():
            try:
                mapped[target] = _get_path(document, source)
            except (KeyError, IndexError, TypeError) as exc:
                if target in {
                    "refused", "refusal_reason", "conversation_id", "trace_id"
                }:
                    continue
                raise PermanentAdapterError(
                    "RESPONSE_MAPPING_FAILED", f"response mapping failed: {exc}"
                ) from exc
        content = mapped.get("content", "")
        if not isinstance(content, (str, dict, list, int, float, bool, type(None))):
            raise PermanentAdapterError(
                "INVALID_RESPONSE", "mapped response content is not JSON-compatible"
            )
        known = {"content", "refused", "refusal_reason", "conversation_id", "trace_id"}
        return AdapterResponse(
            content,
            bool(mapped.get("refused", False)),
            str(mapped.get("refusal_reason", "")),
            str(mapped.get("conversation_id") or conversation.conversation_id),
            str(mapped.get("trace_id", "")),
            {key: value for key, value in mapped.items() if key not in known},
            document,
        )


def redact(value: Any) -> Any:
    """Return a deeply redacted, log-safe copy."""
    if isinstance(value, SecretValue):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]" if _sensitive_key(str(key)) else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        if value.lower().startswith(("bearer ", "basic ")):
            return "[REDACTED]"
        return re.sub(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)"
            r"\s*[:=]\s*([^\s,;]+)",
            lambda match: f"{match.group(1)}=[REDACTED]",
            value,
        )
    return value


def _sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return lowered in {item.replace("-", "_") for item in _SENSITIVE} or any(
        token in lowered for token in ("password", "secret", "api_key", "token")
    )


def _resolved_ip(record: Any) -> str:
    if isinstance(record, (str, ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return str(ipaddress.ip_address(record))
    try:
        return str(ipaddress.ip_address(record[4][0]))
    except (IndexError, TypeError, ValueError) as exc:
        raise PermanentAdapterError(
            "DESTINATION_UNRESOLVED", "resolver returned an invalid address record"
        ) from exc


class _PinnedNetworkBackend:
    """Map only the validated origin hostname to immutable IP addresses."""

    def __init__(self, hostname: str, addresses: tuple[str, ...], delegate: Any) -> None:
        self.hostname = hostname.lower()
        self.addresses = addresses
        self.delegate = delegate

    async def connect_tcp(
        self, host: str, port: int, timeout: float | None = None,
        local_address: str | None = None, socket_options=None,
    ):
        if host.lower() != self.hostname:
            raise PermanentAdapterError(
                "SSRF_BLOCKED", f"connection to unvalidated host {host!r} blocked"
            )
        last_error: Exception | None = None
        for address in self.addresses:
            try:
                return await self.delegate.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None,
        socket_options=None,
    ):
        return await self.delegate.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        await self.delegate.sleep(seconds)


def _pinned_httpx_transport(hostname: str, addresses: tuple[str, ...]):
    try:
        import httpcore
        import httpx
    except ImportError as exc:  # pragma: no cover - guarded by _request
        raise PermanentAdapterError(
            "MISSING_HTTP_DEPENDENCY",
            "HttpAgentAdapter requires the optional 'http' dependency",
        ) from exc
    transport = httpx.AsyncHTTPTransport(retries=0)
    # httpcore retains the original request origin for TLS SNI/certificate
    # verification and Host; only its TCP dial target is replaced.
    transport._pool._network_backend = _PinnedNetworkBackend(  # type: ignore[attr-defined]
        hostname, addresses, httpcore.AnyIOBackend()
    )
    return transport


def _validate_url_shape(url: str, allowed_hosts: tuple[str, ...], allow_http: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise ValueError("HTTP adapter URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HTTP adapter URL must have a host and no user information")
    if allowed_hosts and parsed.hostname.lower() not in allowed_hosts:
        raise ValueError(f"HTTP adapter host {parsed.hostname!r} is not allowlisted")


def _is_unsafe_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not address.is_global or address.is_multicast or address.is_unspecified


def _get_path(value: Any, path: str) -> Any:
    current = value
    if path == "":
        return current
    for component in path.split("."):
        if isinstance(current, Mapping):
            current = current[component]
        elif isinstance(current, (list, tuple)):
            current = current[int(component)]
        else:
            current = getattr(current, component)
    return current


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _decode_stream(raw: bytes) -> Any:
    documents: list[Any] = []
    try:
        lines = raw.decode("utf-8").splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(":"):
                continue
            if stripped.startswith("data:"):
                stripped = stripped[5:].strip()
            if stripped == "[DONE]":
                continue
            documents.append(json.loads(stripped))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermanentAdapterError(
            "INVALID_RESPONSE", "HTTP agent stream is not valid UTF-8 JSON"
        ) from exc
    if not documents:
        raise PermanentAdapterError("INVALID_RESPONSE", "stream contained no JSON data")
    if len(documents) == 1:
        return documents[0]
    if all(isinstance(item, Mapping) and "content" in item for item in documents):
        final = dict(documents[-1])
        final["content"] = "".join(str(item["content"]) for item in documents)
        final["stream_events"] = documents
        return final
    return documents[-1]

"""Fixture-provider lifecycle with secure built-in providers."""

from __future__ import annotations

import ipaddress
import hashlib
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

from blackbox.domain.suite import FixtureRef
from blackbox.domain.trace import ArtifactRef, canonical_json, content_digest


class FixtureError(RuntimeError):
    pass


class DestructiveFixtureApprovalRequired(FixtureError):
    pass


class UnsafeFixtureTarget(FixtureError):
    pass


@runtime_checkable
class FixtureProvider(Protocol):
    destructive: bool

    def prepare(self, config: Mapping[str, Any]) -> Any: ...
    def snapshot(self, state: Any) -> Any: ...
    def verify(self, before: Any, after: Any, config: Mapping[str, Any]) -> Any: ...
    def cleanup(self, state: Any) -> None: ...


@dataclass(frozen=True)
class FixtureExecution:
    name: str
    provider: str
    before: ArtifactRef
    after: ArtifactRef
    verification: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "provider": self.provider,
            "before": self.before.to_dict(), "after": self.after.to_dict(),
            "verification": self.verification,
        }


@dataclass
class InMemoryFixtureProvider:
    """A deterministic test provider wrapping a caller-owned mapping."""

    values: dict[str, Any] = field(default_factory=dict)
    destructive: bool = False

    def prepare(self, config: Mapping[str, Any]) -> dict[str, Any]:
        if "initial" in config:
            self.values.update(dict(config["initial"]))
        return self.values

    def snapshot(self, state: Any) -> Any:
        return json.loads(canonical_json(state))

    def verify(self, before: Any, after: Any, config: Mapping[str, Any]) -> Any:
        expected = config.get("expected")
        return {"passed": expected is None or after == expected,
                "expected": expected, "actual": after, "before": before}

    def cleanup(self, state: Any) -> None:
        return None


def _validate_public_ip(host: str) -> str:
    if not host:
        raise UnsafeFixtureTarget("HTTP fixture URL has no hostname")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise UnsafeFixtureTarget(
            "HTTP fixtures require a literal public IP address; DNS hostnames are "
            "rejected to prevent DNS-rebinding SSRF"
        ) from exc
    if not ip.is_global:
        raise UnsafeFixtureTarget(
            f"HTTP fixture target must be a public address, got {ip}"
        )
    return str(ip)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UnsafeFixtureTarget("HTTP fixture redirects are disabled")


@dataclass
class HTTPFixtureProvider:
    """Read-only HTTP snapshots with SSRF-resistant defaults."""

    destructive: bool = False
    timeout_seconds: float = 10.0
    max_response_bytes: int = 1_048_576

    def prepare(self, config: Mapping[str, Any]) -> dict[str, Any]:
        url = str(config.get("url", ""))
        parsed = urllib.parse.urlsplit(url)
        allow_http = bool(config.get("allow_http", False))
        if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
            raise UnsafeFixtureTarget("HTTP fixtures require HTTPS unless allow_http=true")
        if parsed.username or parsed.password:
            raise UnsafeFixtureTarget("credentials are forbidden in HTTP fixture URLs")
        _validate_public_ip(parsed.hostname or "")
        method = str(config.get("method", "GET")).upper()
        if method not in {"GET", "HEAD"}:
            raise UnsafeFixtureTarget("HTTP fixtures permit only GET and HEAD")
        headers = dict(config.get("headers") or {})
        if any(k.casefold() in {"authorization", "proxy-authorization", "cookie"}
               for k in headers):
            raise UnsafeFixtureTarget("credential headers are forbidden in HTTP fixtures")
        return {"url": url, "method": method, "headers": headers}

    def snapshot(self, state: Mapping[str, Any]) -> Any:
        request = urllib.request.Request(
            state["url"], method=state["method"], headers=state["headers"]
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                body = response.read(self.max_response_bytes + 1)
                if len(body) > self.max_response_bytes:
                    raise FixtureError("HTTP fixture response exceeds size limit")
                return {
                    "status": response.status,
                    "headers": {
                        k.lower(): v for k, v in response.headers.items()
                        if k.lower() in {
                            "content-type", "content-length", "etag",
                            "last-modified", "cache-control",
                        }
                    },
                    "body": body.decode("utf-8", errors="replace"),
                }
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, UnsafeFixtureTarget):
                raise exc.reason
            raise FixtureError(f"HTTP fixture request failed: {exc.reason}") from exc

    def verify(self, before: Any, after: Any, config: Mapping[str, Any]) -> Any:
        expected_status = int(config.get("expected_status", 200))
        return {"passed": after.get("status") == expected_status,
                "expected": expected_status, "actual": after.get("status")}

    def cleanup(self, state: Any) -> None:
        return None


_READ_ONLY_SQL = {"select", "with", "pragma", "explain"}


@dataclass
class SQLReadOnlyFixtureProvider:
    """SQLite snapshots through a read-only connection and statement guard."""

    destructive: bool = False

    def prepare(self, config: Mapping[str, Any]) -> dict[str, Any]:
        database = Path(str(config.get("database", ""))).resolve()
        if not database.is_file():
            raise FixtureError(f"SQL fixture database does not exist: {database}")
        query = str(config.get("query", "")).strip()
        without_trailing = query[:-1] if query.endswith(";") else query
        if not query or ";" in without_trailing or "--" in query or "/*" in query:
            raise FixtureError("SQL fixture must contain exactly one uncommented statement")
        first = query.split(None, 1)[0].casefold()
        if first not in _READ_ONLY_SQL:
            raise FixtureError("SQL fixture permits only read-only SELECT/WITH/PRAGMA/EXPLAIN")
        return {"database": database, "query": query,
                "parameters": tuple(config.get("parameters") or ())}

    def snapshot(self, state: Mapping[str, Any]) -> Any:
        uri = f"{state['database'].as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                connection.execute("PRAGMA query_only=ON")
                cursor = connection.execute(state["query"], state["parameters"])
                columns = [item[0] for item in cursor.description or ()]
                return {"columns": columns, "rows": [list(row) for row in cursor.fetchall()]}
        except sqlite3.Error as exc:
            raise FixtureError(f"SQL fixture query failed: {exc}") from exc

    def verify(self, before: Any, after: Any, config: Mapping[str, Any]) -> Any:
        expected = config.get("expected")
        return {"passed": expected is None or after == expected,
                "expected": expected, "actual": after}

    def cleanup(self, state: Any) -> None:
        return None


@dataclass
class FilesystemSandboxFixtureProvider:
    """Snapshots files confined beneath a configured sandbox root."""

    root: Path
    destructive: bool = False
    max_file_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        if not self.root.is_dir():
            raise FixtureError(f"filesystem sandbox does not exist: {self.root}")

    def _confine(self, value: str) -> Path:
        candidate = (self.root / value).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise UnsafeFixtureTarget(f"path escapes filesystem sandbox: {value!r}") from exc
        return candidate

    def prepare(self, config: Mapping[str, Any]) -> dict[str, Any]:
        return {"path": self._confine(str(config.get("path", ".")))}

    def snapshot(self, state: Mapping[str, Any]) -> Any:
        path = state["path"]
        if not path.exists():
            return {"exists": False}
        if path.is_symlink():
            raise UnsafeFixtureTarget("filesystem fixture refuses symbolic links")
        if path.is_file():
            size = path.stat().st_size
            if size > self.max_file_bytes:
                raise FixtureError("filesystem fixture file exceeds size limit")
            return {"exists": True, "type": "file", "size": size,
                    "digest": hashlib.sha256(path.read_bytes()).hexdigest()}
        entries = []
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                raise UnsafeFixtureTarget("filesystem fixture refuses symbolic links")
            if child.is_file():
                entries.append({
                    "path": str(child.relative_to(path)), "size": child.stat().st_size,
                    "digest": hashlib.sha256(child.read_bytes()).hexdigest(),
                })
        return {"exists": True, "type": "directory", "entries": entries}

    def verify(self, before: Any, after: Any, config: Mapping[str, Any]) -> Any:
        expected_digest = config.get("expected_digest")
        return {"passed": expected_digest is None or after.get("digest") == expected_digest,
                "expected": expected_digest, "actual": after}

    def cleanup(self, state: Any) -> None:
        return None


class FixtureManager:
    """Runs lifecycle operations and always returns auditable snapshot references."""

    def __init__(self, providers: Mapping[str, FixtureProvider]):
        self.providers = dict(providers)

    @staticmethod
    def _artifact(name: str, stage: str, snapshot: Any) -> ArtifactRef:
        payload = canonical_json(snapshot).encode("utf-8")
        return ArtifactRef(
            uri=f"fixture://{name}/{stage}", digest=content_digest(snapshot),
            media_type="application/json", size_bytes=len(payload),
            name=f"{name}-{stage}",
        )

    def run(
        self, name: str, provider_name: str, config: Mapping[str, Any],
        operation: Any, *, approve_destructive: bool = False,
        destructive: bool = False,
    ) -> FixtureExecution:
        try:
            provider = self.providers[provider_name]
        except KeyError as exc:
            raise FixtureError(f"unknown fixture provider {provider_name!r}") from exc
        is_destructive = destructive or bool(config.get("destructive", False)) or provider.destructive
        if is_destructive and not approve_destructive:
            raise DestructiveFixtureApprovalRequired(
                f"fixture {name!r} is destructive; explicit approval is required"
            )
        state = provider.prepare(config)
        primary_error: Exception | None = None
        try:
            before_value = provider.snapshot(state)
            operation(state)
            after_value = provider.snapshot(state)
            verification = provider.verify(before_value, after_value, config)
            return FixtureExecution(
                name, provider_name, self._artifact(name, "before", before_value),
                self._artifact(name, "after", after_value), verification,
            )
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            try:
                provider.cleanup(state)
            except Exception as cleanup_error:
                if primary_error is not None:
                    raise ExceptionGroup(
                        "fixture execution and cleanup failed",
                        [primary_error, cleanup_error],
                    ) from None
                raise FixtureError(
                    f"fixture cleanup failed: {cleanup_error}"
                ) from cleanup_error

    def run_many(
        self, fixtures: Iterable[FixtureRef], operation: Callable[[], Any],
        *, approve_destructive: bool = False,
    ) -> tuple[Any, list[FixtureExecution]]:
        """Prepare all fixtures, execute once, verify all, and clean up in reverse."""
        prepared: list[tuple[FixtureRef, FixtureProvider, Any, Any]] = []
        primary_error: Exception | None = None
        try:
            for index, fixture in enumerate(fixtures):
                try:
                    provider = self.providers[fixture.provider]
                except KeyError as exc:
                    raise FixtureError(
                        f"unknown fixture provider {fixture.provider!r}"
                    ) from exc
                name = fixture.name or f"{fixture.provider}-{index + 1}"
                destructive = (
                    fixture.destructive
                    or bool(fixture.config.get("destructive", False))
                    or provider.destructive
                )
                if destructive and not approve_destructive:
                    raise DestructiveFixtureApprovalRequired(
                        f"fixture {name!r} is destructive; explicit approval is required"
                    )
                state = provider.prepare(fixture.config)
                prepared.append((fixture, provider, state, None))
                before = provider.snapshot(state)
                prepared[-1] = (fixture, provider, state, before)

            result = operation()
            executions: list[FixtureExecution] = []
            for index, (fixture, provider, state, before) in enumerate(prepared):
                name = fixture.name or f"{fixture.provider}-{index + 1}"
                after = provider.snapshot(state)
                verification = provider.verify(before, after, fixture.config)
                executions.append(FixtureExecution(
                    name, fixture.provider,
                    self._artifact(name, "before", before),
                    self._artifact(name, "after", after),
                    verification,
                ))
            return result, executions
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            cleanup_errors: list[Exception] = []
            for _, provider, state, _ in reversed(prepared):
                try:
                    provider.cleanup(state)
                except Exception as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                if primary_error is not None:
                    raise ExceptionGroup(
                        "fixture execution and cleanup failed",
                        [primary_error, *cleanup_errors],
                    ) from None
                raise FixtureError(
                    "fixture cleanup failed: "
                    + "; ".join(str(error) for error in cleanup_errors)
                ) from cleanup_errors[0]


# Descriptive aliases retained for integrations that omit the security qualifier.
HTTPFixture = HTTPFixtureProvider
SQLFixtureProvider = SQLReadOnlyFixtureProvider
FilesystemFixtureProvider = FilesystemSandboxFixtureProvider
InMemoryFixture = InMemoryFixtureProvider


def default_fixture_manager(*, filesystem_root: str | Path | None = None) -> FixtureManager:
    """Return secure built-ins; filesystem access is opt-in and root-confined."""
    providers: dict[str, FixtureProvider] = {
        "http": HTTPFixtureProvider(),
        "sql": SQLReadOnlyFixtureProvider(),
        "memory": InMemoryFixtureProvider(),
    }
    if filesystem_root is not None:
        providers["filesystem"] = FilesystemSandboxFixtureProvider(
            Path(filesystem_root)
        )
    return FixtureManager(providers)

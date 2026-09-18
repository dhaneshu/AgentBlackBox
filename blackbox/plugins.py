"""Typed plugin contracts and Python entry-point discovery."""

from __future__ import annotations

from importlib import metadata
from typing import Any, ClassVar, Protocol, TypeVar, runtime_checkable

PLUGIN_API_VERSION = "2.0"
PLUGIN_GROUPS = {
    "adapters": "blackbox.adapters",
    "evaluators": "blackbox.evaluators",
    "policies": "blackbox.policies",
    "artifact_stores": "blackbox.artifact_stores",
    "fixture_providers": "blackbox.fixture_providers",
    "price_providers": "blackbox.price_providers",
    "exporters": "blackbox.exporters",
}


class PluginError(RuntimeError):
    """Base plugin loading error."""


class DuplicatePluginError(PluginError):
    """Raised when two entry points claim the same public name."""


class IncompatiblePluginVersionError(PluginError):
    """Raised when a plugin targets another contract major version."""


@runtime_checkable
class Plugin(Protocol):
    name: ClassVar[str]
    api_version: ClassVar[str]


@runtime_checkable
class AdapterPlugin(Plugin, Protocol):
    def run(self, request: Any) -> Any: ...


@runtime_checkable
class EvaluatorPlugin(Plugin, Protocol):
    version: ClassVar[str]
    input_requirements: ClassVar[tuple[Any, ...]]

    def evaluate(self, context: Any) -> Any: ...

    async def evaluate_async(self, context: Any) -> Any: ...


@runtime_checkable
class PolicyPlugin(Plugin, Protocol):
    version: ClassVar[str]
    configuration_schema: ClassVar[dict[str, Any]]
    severity: ClassVar[str]
    remediation: ClassVar[str]

    def applicable(self, trace: Any, configuration: Any) -> bool: ...

    def evaluate(self, trace: Any, configuration: Any) -> Any: ...


@runtime_checkable
class ArtifactStorePlugin(Plugin, Protocol):
    def put(self, name: str, content: bytes) -> str: ...

    def get(self, reference: str) -> bytes: ...


@runtime_checkable
class FixtureProviderPlugin(Plugin, Protocol):
    destructive: ClassVar[bool]

    def prepare(self, fixture: dict[str, Any]) -> Any: ...

    def snapshot(self, state: Any) -> Any: ...

    def verify(self, before: Any, after: Any, fixture: dict[str, Any]) -> Any: ...

    def cleanup(self, state: Any) -> None: ...


@runtime_checkable
class PriceProviderPlugin(Plugin, Protocol):
    def price(self, model: str, usage: Any) -> Any: ...


@runtime_checkable
class ExporterPlugin(Plugin, Protocol):
    def export(self, value: Any) -> bytes | str: ...


T = TypeVar("T")


def discover(group: str, *, entry_points: Any = None) -> dict[str, Any]:
    """Load one plugin category, rejecting ambiguous or incompatible plugins."""
    entry_point_group = PLUGIN_GROUPS.get(group, group)
    available = metadata.entry_points() if entry_points is None else entry_points
    selected = (
        available.select(group=entry_point_group)
        if hasattr(available, "select")
        else available.get(entry_point_group, ())
    )
    plugins: dict[str, Any] = {}
    for entry_point in selected:
        if entry_point.name in plugins:
            raise DuplicatePluginError(
                f"Duplicate plugin {entry_point.name!r} in {entry_point_group!r}"
            )
        plugin = entry_point.load()
        version = str(getattr(plugin, "api_version", ""))
        if version.split(".", 1)[0] != PLUGIN_API_VERSION.split(".", 1)[0]:
            raise IncompatiblePluginVersionError(
                f"Plugin {entry_point.name!r} uses API {version or '<missing>'}; "
                f"expected compatible API {PLUGIN_API_VERSION}"
            )
        plugins[entry_point.name] = plugin
    return plugins


def discover_all(*, entry_points: Any = None) -> dict[str, dict[str, Any]]:
    return {
        category: discover(category, entry_points=entry_points)
        for category in PLUGIN_GROUPS
    }

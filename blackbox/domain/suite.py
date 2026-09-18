"""Versioned evaluation cases and immutable dataset utilities."""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from blackbox.domain.schema import CURRENT_SCHEMA_VERSION, migrate_document
from blackbox.domain.trace import Message, canonical_json, content_digest

EXPECTATIONS: tuple[str, ...] = ("answer", "refuse")
_PARAMETER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")


@dataclass(frozen=True)
class FixtureRef:
    """A named fixture used by a case."""

    provider: str
    config: Mapping[str, Any] = field(default_factory=dict)
    name: str = ""
    destructive: bool = False

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | str) -> "FixtureRef":
        if isinstance(value, str):
            return cls(provider=value, name=value)
        return cls(
            provider=str(value.get("provider", "")),
            config=dict(value.get("config") or {}),
            name=str(value.get("name", "")),
            destructive=bool(value.get("destructive", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "name": self.name,
            "destructive": self.destructive, "config": dict(self.config),
        }


@dataclass(frozen=True)
class Assertion:
    """Serializable declaration consumed by the deterministic assertion engine."""

    type: str
    expected: Any = None
    selector: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)
    assertion_id: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | str) -> "Assertion":
        if isinstance(value, str):
            return cls(type=value, expected=True if value == "refusal" else None)
        known = {"type", "kind", "expected", "selector", "jsonpath", "options", "id"}
        options = dict(value.get("options") or {})
        options.update({k: v for k, v in value.items() if k not in known})
        return cls(
            type=str(value.get("type") or value.get("kind") or ""),
            expected=value.get(
                "expected", True if value.get("type") == "refusal" else None
            ),
            selector=str(value.get("selector") or value.get("jsonpath") or ""),
            options=options,
            assertion_id=str(value.get("id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {"type": self.type, "expected": self.expected}
        if self.assertion_id:
            result["id"] = self.assertion_id
        if self.selector:
            result["selector"] = self.selector
        if self.options:
            result["options"] = dict(self.options)
        return result


@dataclass(frozen=True)
class EvaluationCase:
    """A multi-turn evaluation input and its expected deterministic outcomes."""

    id: str
    messages: tuple[Message, ...]
    variables: Mapping[str, Any] = field(default_factory=dict)
    fixture_refs: tuple[FixtureRef, ...] = ()
    tags: tuple[str, ...] = ()
    expected_outcome: str = "answer"
    assertions: tuple[Assertion, ...] = ()
    evaluators: tuple[Mapping[str, Any], ...] = ()
    timeout_seconds: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = CURRENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvaluationCase":
        data = migrate_document("suite", dict(raw))
        messages = data.get("messages")
        if messages is None:
            messages = [{"role": "user", "content": data.get("question", "")}]
        assertions = list(data.get("assertions") or [])
        # Version-1/legacy fields remain a lossless public compatibility path.
        if not assertions:
            assertions.extend(
                {"type": "text_contains", "expected": value}
                for value in data.get("must_contain") or ()
            )
            assertions.extend(
                {"type": "citation", "expected": value}
                for value in data.get("must_cite") or ()
            )
            assertions.extend(
                {"type": "text_not_contains", "expected": value}
                for value in data.get("forbid") or ()
            )
            if str(data.get("expect", "")).lower() == "refuse":
                assertions.append({"type": "refusal", "expected": True})
        metadata = dict(data.get("metadata") or {})
        if data.get("notes") and "notes" not in metadata:
            metadata["notes"] = data["notes"]
        timeout = data.get("timeout_seconds", data.get("timeout"))
        return cls(
            id=str(data.get("id", "")).strip(),
            messages=tuple(Message.from_dict(v) for v in messages),
            variables=dict(data.get("variables") or {}),
            fixture_refs=tuple(
                FixtureRef.from_dict(v) for v in
                (data.get("fixture_refs") or data.get("fixtures") or ())
            ),
            tags=tuple(str(v) for v in data.get("tags") or ()),
            expected_outcome=str(
                data.get("expected_outcome", data.get("expect", "answer"))
            ).strip().lower(),
            assertions=tuple(Assertion.from_dict(v) for v in assertions),
            evaluators=tuple(dict(v) if isinstance(v, Mapping) else {"name": str(v)}
                             for v in data.get("evaluators") or ()),
            timeout_seconds=float(timeout) if timeout is not None else None,
            metadata=metadata,
            schema_version=str(data["schema_version"]),
        )

    @property
    def question(self) -> str:
        users = [m for m in self.messages if m.role == "user"]
        return str(users[-1].content) if users else ""

    @property
    def expect(self) -> str:
        return self.expected_outcome

    @property
    def must_contain(self) -> tuple[str, ...]:
        return tuple(str(a.expected) for a in self.assertions if a.type == "text_contains")

    @property
    def must_cite(self) -> tuple[str, ...]:
        return tuple(str(a.expected) for a in self.assertions if a.type == "citation")

    @property
    def forbid(self) -> tuple[str, ...]:
        return tuple(str(a.expected) for a in self.assertions
                     if a.type == "text_not_contains")

    @property
    def notes(self) -> str:
        return str(self.metadata.get("notes", ""))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "messages": [m.to_dict() for m in self.messages],
            "variables": dict(self.variables),
            "fixture_refs": [v.to_dict() for v in self.fixture_refs],
            "tags": list(self.tags),
            "expected_outcome": self.expected_outcome,
            "assertions": [v.to_dict() for v in self.assertions],
            "evaluators": [dict(v) for v in self.evaluators],
            "timeout_seconds": self.timeout_seconds,
            "metadata": dict(self.metadata),
        }

    def parameterize(self, variables: Mapping[str, Any]) -> "EvaluationCase":
        merged = {**self.variables, **variables}

        def replace(value: Any) -> Any:
            if isinstance(value, str):
                match = _PARAMETER.fullmatch(value)
                if match:
                    if match.group(1) not in merged:
                        raise ValueError(f"{self.id}: unknown parameter {match.group(1)!r}")
                    return merged[match.group(1)]
                def sub(found: re.Match[str]) -> str:
                    if found.group(1) not in merged:
                        raise ValueError(f"{self.id}: unknown parameter {found.group(1)!r}")
                    return str(merged[found.group(1)])
                return _PARAMETER.sub(sub, value)
            if isinstance(value, list):
                return [replace(v) for v in value]
            if isinstance(value, dict):
                return {k: replace(v) for k, v in value.items()}
            return value

        document = replace(self.to_dict())
        document["variables"] = merged
        return EvaluationCase.from_dict(document)


class Case(EvaluationCase):
    """Legacy constructor retained for existing SDK and CLI callers."""

    def __init__(
        self, id: str, question: str, expect: str = "answer",
        must_contain: Iterable[str] = (), must_cite: Iterable[str] = (),
        forbid: Iterable[str] = (), notes: str = "",
        schema_version: str = CURRENT_SCHEMA_VERSION,
    ):
        assertions = (
            *(Assertion("text_contains", v) for v in must_contain),
            *(Assertion("citation", v) for v in must_cite),
            *(Assertion("text_not_contains", v) for v in forbid),
        )
        if expect == "refuse":
            assertions = (*assertions, Assertion("refusal", True))
        super().__init__(
            id=id, messages=(Message("user", question),),
            expected_outcome=expect, assertions=tuple(assertions),
            metadata={"notes": notes} if notes else {},
            schema_version=schema_version,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationCase:
        return EvaluationCase.from_dict(data)


@dataclass
class SuiteReport:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class SamplingConfig:
    operation: str
    seed: int
    selected_case_ids: tuple[str, ...]
    size: int | None = None
    fraction: float | None = None
    split: str = ""
    ratios: Mapping[str, float] = field(default_factory=dict)
    provenance_selected_case_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation, "seed": self.seed, "size": self.size,
            "fraction": self.fraction, "split": self.split,
            "ratios": dict(self.ratios),
            "selected_case_ids": list(self.selected_case_ids),
            "provenance_selected_case_ids": list(self.provenance_selected_case_ids),
        }


@dataclass(frozen=True)
class Dataset:
    cases: tuple[EvaluationCase, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    sampling: SamplingConfig | None = None
    schema_version: str = CURRENT_SCHEMA_VERSION

    @property
    def version_hash(self) -> str:
        cases = []
        for case in self.cases:
            value = case.to_dict()
            for message in value["messages"]:
                # Platform-assigned transport fields do not alter dataset semantics.
                message.pop("message_id", None)
                message.pop("created_at", None)
                for artifact in message.get("artifact_refs", []):
                    artifact.pop("artifact_id", None)
            cases.append(value)
        return content_digest({
            "schema_version": self.schema_version,
            "cases": cases,
            "metadata": dict(self.metadata),
        })

    @property
    def dataset_version_hash(self) -> str:
        return self.version_hash

    @property
    def selected_case_ids(self) -> tuple[str, ...]:
        return tuple(case.id for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_version_hash": self.version_hash,
            "cases": [case.to_dict() for case in self.cases],
            "metadata": dict(self.metadata),
            "source": self.source,
            "sampling": self.sampling.to_dict() if self.sampling else None,
            "selected_case_ids": list(self.selected_case_ids),
        }

    def filter(
        self, *, include_tags: Iterable[str] = (), exclude_tags: Iterable[str] = (),
        case_ids: Iterable[str] = (),
    ) -> "Dataset":
        include, exclude, ids = set(include_tags), set(exclude_tags), set(case_ids)
        cases = tuple(c for c in self.cases
                      if (not include or include.issubset(set(c.tags)))
                      and not exclude.intersection(c.tags)
                      and (not ids or c.id in ids))
        sampling = self.sampling
        if sampling is not None:
            provenance = (
                sampling.provenance_selected_case_ids or sampling.selected_case_ids
            )
            sampling = SamplingConfig(
                sampling.operation, sampling.seed, tuple(c.id for c in cases),
                sampling.size, sampling.fraction, sampling.split, sampling.ratios,
                provenance,
            )
        return Dataset(cases, self.metadata, self.source, sampling)

    def sample(
        self, *, size: int | None = None, fraction: float | None = None, seed: int = 0,
    ) -> "Dataset":
        if (size is None) == (fraction is None):
            raise ValueError("specify exactly one of size or fraction")
        if fraction is not None and not 0 <= fraction <= 1:
            raise ValueError("fraction must be between 0 and 1")
        count = size if size is not None else round(len(self.cases) * float(fraction))
        if count is None or count < 0 or count > len(self.cases):
            raise ValueError("sample size is outside the dataset")
        indexes = sorted(random.Random(seed).sample(range(len(self.cases)), count))
        cases = tuple(self.cases[index] for index in indexes)
        config = SamplingConfig("sample", seed, tuple(c.id for c in cases), size, fraction)
        return Dataset(cases, self.metadata, self.source, config)

    def split(
        self, ratios: Mapping[str, float] | None = None, *, seed: int = 0,
    ) -> dict[str, "Dataset"]:
        ratios = dict(ratios or {"train": .8, "validation": .1, "test": .1})
        if not ratios or any(v < 0 for v in ratios.values()) or sum(ratios.values()) <= 0:
            raise ValueError("split ratios must be non-negative with a positive total")
        names = list(ratios)
        total = sum(ratios.values())
        shuffled = list(self.cases)
        random.Random(seed).shuffle(shuffled)
        boundaries: list[int] = []
        running = 0.0
        for name in names[:-1]:
            running += ratios[name] / total
            boundaries.append(round(len(shuffled) * running))
        chunks, start = {}, 0
        for name, end in zip(names, [*boundaries, len(shuffled)]):
            cases = tuple(shuffled[start:end])
            config = SamplingConfig(
                "split", seed, tuple(c.id for c in cases), split=name, ratios=ratios
            )
            chunks[name] = Dataset(cases, self.metadata, self.source, config)
            start = end
        return chunks


def _read_document(path: Path) -> Any:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".jsonl":
        values = []
        for line_no, raw in enumerate(text.splitlines(), 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                values.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON ({exc.msg})") from exc
        return values
    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON ({exc.msg})") from exc
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("YAML suites require PyYAML; install agent-black-box") from exc
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: invalid YAML ({exc})") from exc
    raise ValueError(f"unsupported suite format {suffix!r}; use .jsonl, .json, .yaml, or .yml")


def load_dataset(
    path: str | Path, *, include_tags: Iterable[str] = (),
    exclude_tags: Iterable[str] = (), case_ids: Iterable[str] = (),
    validate: bool = True,
    _stack: tuple[Path, ...] = (),
) -> Dataset:
    target = Path(path).resolve()
    if not target.exists():
        raise FileNotFoundError(f"Suite not found: {target}")
    if target in _stack:
        raise ValueError(f"suite composition cycle: {' -> '.join(map(str, (*_stack, target)))}")
    document = _read_document(target)
    if isinstance(document, list):
        raw_cases, metadata, includes, parameters = document, {}, [], [{}]
    elif isinstance(document, dict):
        raw_cases = (
            [document] if "id" in document and "cases" not in document
            else document.get("cases") or []
        )
        metadata = dict(document.get("metadata") or {})
        includes = document.get("include") or document.get("includes") or []
        parameters = document.get("parameters") or [{}]
        if isinstance(includes, str):
            includes = [includes]
        if isinstance(parameters, Mapping):
            parameters = [parameters]
    else:
        raise ValueError(f"{target}: suite root must be an object or array")
    cases: list[EvaluationCase] = []
    for include in includes:
        child = load_dataset(
            target.parent / str(include), validate=validate, _stack=(*_stack, target)
        )
        cases.extend(child.cases)
    for raw in raw_cases:
        raw = dict(raw)
        case_parameters = raw.pop("parameters", None)
        if isinstance(case_parameters, Mapping):
            case_parameters = [case_parameters]
        selected_parameters = case_parameters or parameters
        base = EvaluationCase.from_dict(raw)
        for parameter_set in selected_parameters:
            case = base.parameterize(parameter_set) if parameter_set else base
            if len(selected_parameters) > 1 and case.id == base.id:
                suffix = content_digest(parameter_set)[:8]
                value = case.to_dict()
                value["id"] = f"{case.id}-{suffix}"
                case = EvaluationCase.from_dict(value)
            cases.append(case)
    dataset = Dataset(tuple(cases), metadata, str(target))
    if validate:
        report = validate_suite(list(dataset.cases))
        if not report.ok:
            raise ValueError(f"{target}: invalid suite: {'; '.join(report.errors)}")
        for case in dataset.cases:
            validate_case_schema(case)
    return dataset.filter(
        include_tags=include_tags, exclude_tags=exclude_tags, case_ids=case_ids
    )


def load_suite(path: str | Path, **filters: Any) -> list[EvaluationCase]:
    """Compatibility API returning cases while accepting all suite formats."""
    return list(load_dataset(path, validate=False, **filters).cases)


def validate_case_schema(case: EvaluationCase) -> None:
    """Validate a serialized case against the bundled Draft 2020-12 schema."""
    try:
        import jsonschema
    except ImportError as exc:
        raise RuntimeError(
            "suite schema validation requires the 'sdk' extra"
        ) from exc
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "suite-2.0.schema.json"
    if not schema_path.is_file():
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "suite-2.0.schema.json"
    if not schema_path.is_file():
        raise RuntimeError("bundled suite schema is missing")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(case.to_dict())


def validate_suite(cases: list[EvaluationCase], corpus=None) -> SuiteReport:
    report, seen = SuiteReport(), set()
    if not cases:
        report.errors.append("suite has no cases")
    outcome_assertions = {
        "text", "text_exact", "text_contains", "text_not_contains", "regex",
        "refusal", "json_schema", "numeric_tolerance", "citation",
    }
    for case in cases:
        where = case.id or "<no id>"
        if not case.id:
            report.errors.append("a case has no id")
        elif case.id in seen:
            report.errors.append(f"{where}: duplicate id")
        seen.add(case.id)
        if not case.messages:
            report.errors.append(f"{where}: no messages")
        if not case.question:
            report.errors.append(f"{where}: empty question")
        if case.expect not in EXPECTATIONS:
            report.errors.append(
                f"{where}: expected_outcome {case.expect!r} is not one of "
                f"{', '.join(EXPECTATIONS)}"
            )
        if case.expect == "answer" and not any(
            assertion.type in outcome_assertions for assertion in case.assertions
        ):
            report.errors.append(
                f"{where}: a case expecting an answer must assert something with "
                "at least one outcome assertion"
            )
        if case.expect == "refuse" and any(
            a.type in {"text_contains", "citation"} for a in case.assertions
        ):
            report.errors.append(
                f"{where}: a case expecting a refusal cannot also require content or citations"
            )
        if corpus is not None:
            known = {p.source for p in corpus.passages}
            for wanted in case.must_cite:
                if not any(source.endswith(wanted) or wanted in source for source in known):
                    report.errors.append(f"{where}: must_cite {wanted!r} is not in the corpus")
    return report


def summarise(cases: list[EvaluationCase]) -> str:
    answerable = sum(1 for c in cases if c.expect == "answer")
    return f"{len(cases)} case(s): {answerable} answerable, {len(cases)-answerable} that must be refused"

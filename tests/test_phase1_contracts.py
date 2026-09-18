"""Phase 1 package, schema, configuration, and plugin contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import jsonschema
import pytest

from blackbox.config import AzureOpenAISettings, RemoteProfile
from blackbox.domain.result import CaseResult, RunScore
from blackbox.domain.schema import (
    CURRENT_SCHEMA_VERSION,
    UnsupportedSchemaVersionError,
    evaluation_result_identifier,
    migrate_document,
)
from blackbox.domain.suite import Case
from blackbox.domain.trace import Citation, Trace
from blackbox.plugins import (
    DuplicatePluginError,
    IncompatiblePluginVersionError,
    discover,
)

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("kind", ["trace", "suite", "result"])
def test_golden_fixture_validates_against_schema(kind):
    schema = json.loads((ROOT / "schemas" / f"{kind}-2.0.schema.json").read_text())
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "schemas" / f"{kind}-2.0.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(fixture)


def test_current_models_serialize_as_schema_v2():
    trace = Trace("run", "case", "agent", "question")
    case = Case("case", "question", must_contain=("answer",))
    score = RunScore(
        run_id="run",
        agent="agent",
        results=[CaseResult("case", "correct", "ok", True, 0, 0)],
    )

    assert trace.to_dict()["schema_version"] == CURRENT_SCHEMA_VERSION
    assert case.to_dict()["schema_version"] == CURRENT_SCHEMA_VERSION
    assert score.to_dict()["schema_version"] == CURRENT_SCHEMA_VERSION
    assert score.to_dict()["results"][0]["evaluation_result_id"] == (
        evaluation_result_identifier("run", "case")
    )

    for kind, document in (
        ("trace", trace.to_dict()),
        ("suite", case.to_dict()),
        ("result", score.to_dict()),
    ):
        schema = json.loads(
            (ROOT / "schemas" / f"{kind}-2.0.schema.json").read_text()
        )
        jsonschema.Draft202012Validator(schema).validate(document)


def test_legacy_positional_constructors_remain_compatible():
    trace = Trace("run", "case", "agent", "question", "answer", True)
    case = Case("case", "question", "refuse")
    score = RunScore("run", "agent", [], 1.0)

    assert (trace.answer, trace.refused) == ("answer", True)
    assert case.expect == "refuse"
    assert score.determinism == 1.0


def test_v1_trace_migration_preserves_evaluation_evidence():
    legacy = {
        "run_id": "run",
        "case_id": "case",
        "agent": "agent",
        "question": "question",
        "answer": "answer",
        "refused": False,
        "citations": [{
            "source": "plan.md", "lines": [1, 1], "claim": "answer", "support": 1
        }],
        "violations": [{
            "policy": "EXAMPLE", "severity": "low", "detail": "detail"
        }],
        "tokens_in": 2,
        "tokens_out": 3,
        "config": {"floor": 0.5},
    }
    expected_hash = Trace.from_dict(legacy).config_hash
    migrated = migrate_document("trace", legacy)
    restored = Trace.from_dict(migrated)

    assert restored.answer == "answer"
    assert restored.citations == [
        Citation("plan.md", (1, 1), "", "answer", 1.0)
    ]
    assert restored.violations[0].policy == "EXAMPLE"
    assert restored.total_tokens == 5
    assert restored.config_hash == expected_hash
    assert restored.conversation_id == "conversation-run-case"


def test_v1_result_migration_produces_valid_schema_v2_without_mutating_input():
    legacy_result = {
        "case_id": "case",
        "verdict": "correct",
        "reason": "ok",
        "grounded": True,
        "violations": 0,
        "tokens": 5,
    }
    legacy = {
        "generated_at": "2026-09-18T00:00:00+00:00",
        "run_id": "run",
        "agent": "agent",
        "total": 1,
        "accuracy": 1.0,
        "verdicts": {
            "correct": 1,
            "correctly_refused": 0,
            "hallucinated": 0,
            "over_refused": 0,
            "wrong": 0,
        },
        "hallucination_rate": 0.0,
        "over_refusal_rate": 0.0,
        "groundedness": 1.0,
        "violations": 0,
        "tokens": 5,
        "determinism": 1.0,
        "results": [legacy_result],
    }

    migrated = migrate_document("result", legacy)
    schema = json.loads((ROOT / "schemas" / "result-2.0.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(migrated)

    assert migrated["results"][0]["schema_version"] == CURRENT_SCHEMA_VERSION
    assert migrated["results"][0]["evaluation_result_id"] == (
        evaluation_result_identifier("run", "case")
    )
    assert "schema_version" not in legacy_result
    assert "evaluation_result_id" not in legacy_result


def test_evaluation_result_identity_is_stable_unique_and_shared_by_migration():
    first = RunScore(
        run_id="run-a",
        results=[CaseResult("case", "correct", "ok", True, 0, 0)],
    ).results[0]
    repeated = RunScore(
        run_id="run-a",
        results=[CaseResult("case", "correct", "ok", True, 0, 0)],
    ).results[0]
    other_run = RunScore(
        run_id="run-b",
        results=[CaseResult("case", "correct", "ok", True, 0, 0)],
    ).results[0]
    migrated = migrate_document("result", {
        "run_id": "run-a",
        "results": [{
            "case_id": "case",
            "evaluation_result_id": "legacy-id",
        }],
    })

    assert first.evaluation_result_id == repeated.evaluation_result_id
    assert first.evaluation_result_id != other_run.evaluation_result_id
    assert migrated["results"][0]["evaluation_result_id"] == (
        first.evaluation_result_id
    )


def test_unknown_schema_version_is_rejected():
    with pytest.raises(UnsupportedSchemaVersionError, match="9.0"):
        migrate_document("trace", {"schema_version": "9.0"})


def test_compatibility_exports_are_identical():
    with pytest.warns(DeprecationWarning):
        from importlib import reload
        import blackbox.trace as compatibility
        reload(compatibility)

    assert compatibility.Trace is Trace


def test_azure_settings_load_dotenv_and_exclude_secret(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "AZURE_OPENAI_ENDPOINT=https://example.test/\n"
        "OPENAI_DEPLOYMENT=deployment\n"
        "AZURE_OPENAI_AUTH=api_key\n"
        "AZURE_OPENAI_API_KEY=do-not-serialize\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    settings = AzureOpenAISettings(_env_file=env)

    assert settings.endpoint == "https://example.test/"
    assert settings.require_runtime() == "deployment"
    assert "api_key" not in settings.public_dict()
    assert "do-not-serialize" not in str(settings.public_dict())


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "AZURE_OPENAI_ENDPOINT=https://file.test/\nOPENAI_DEPLOYMENT=file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_DEPLOYMENT", "environment")
    settings = AzureOpenAISettings(_env_file=env)
    assert settings.deployment == "environment"


def test_remote_profile_is_typed_and_hides_token():
    profile = RemoteProfile(
        url="https://blackbox.test",
        workspace_id="workspace",
        project_id="project",
        access_token="secret",
    )
    assert profile.kind == "remote"
    assert "access_token" not in profile.public_dict()


@dataclass
class _EntryPoint:
    name: str
    group: str
    plugin: object

    def load(self):
        return self.plugin


class _EntryPoints(list):
    def select(self, *, group):
        return [entry for entry in self if entry.group == group]


def test_plugin_discovery_loads_a_compatible_entry_point():
    class Adapter:
        api_version = "2.4"

    found = discover(
        "adapters",
        entry_points=_EntryPoints([_EntryPoint("sample", "blackbox.adapters", Adapter)]),
    )
    assert found == {"sample": Adapter}


def test_plugin_discovery_rejects_duplicates_and_wrong_major():
    class Compatible:
        api_version = "2.0"

    duplicate = _EntryPoints([
        _EntryPoint("same", "blackbox.adapters", Compatible),
        _EntryPoint("same", "blackbox.adapters", Compatible),
    ])
    with pytest.raises(DuplicatePluginError, match="same"):
        discover("adapters", entry_points=duplicate)

    class Incompatible:
        api_version = "1.0"

    with pytest.raises(IncompatiblePluginVersionError, match="1.0"):
        discover(
            "adapters",
            entry_points=_EntryPoints([
                _EntryPoint("old", "blackbox.adapters", Incompatible)
            ]),
        )


def test_azure_agent_has_no_direct_environment_reads():
    source = (ROOT / "blackbox" / "azure_agent.py").read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "os.getenv" not in source

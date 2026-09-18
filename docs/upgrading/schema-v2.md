# Upgrading to schema 2.0

Schema 2.0 makes trace, suite, and result versions explicit and introduces
stable workspace, project, dataset, experiment, conversation, turn, and
evaluation-result identifiers.

## Python imports

Existing imports continue to work but emit `DeprecationWarning`:

```python
from blackbox.trace import Trace
from blackbox.suite import Case
```

New code should use the platform-independent package:

```python
from blackbox.domain.trace import Trace
from blackbox.domain.suite import Case
from blackbox.domain.result import RunScore
```

`blackbox.recorder.Recorder`, `python -m blackbox`, and the `blackbox` console
command retain their existing single-turn behavior during the compatibility
period. The recorder also provides sync and async `conversation()`, `turn()`,
and `span()` contexts. These contexts capture exceptions and nanosecond timing,
and use `contextvars` so sibling async tools and agents inherit the correct
parent span without sharing mutable context stacks.

## JSON and JSONL

Version 1 records have no `schema_version`. Loaders recognize these records,
emit `DeprecationWarning`, and migrate them in memory without modifying the
source file. Saving the loaded objects writes schema 2.0. Unknown versions fail
with `UnsupportedSchemaVersionError`; they are never guessed.

Trace migration preserves answers, refusals, citations, violations, token
counts, configuration, and configuration hashes. Missing identifiers receive
deterministic local compatibility values derived from the run and case IDs.
New platform-generated identifiers are ULIDs. Ingested W3C 32-character trace
IDs and 16-character span IDs remain valid.

Rich traces can contain conversations, turns, messages, spans, links,
discriminated events, handoffs, artifact references, usage, costs, and captured
errors. Use `iter_traces()` and `JSONLWriter` for bounded-memory JSONL
processing. Event/message payload limits default to 1 MiB and failures report
the source line and event identifier when available.

Validate persisted data against:

- `schemas/trace-2.0.schema.json`
- `schemas/suite-2.0.schema.json`
- `schemas/result-2.0.schema.json`

The version 1 migration creates an empty distributed graph; it does not invent
conversation turns or spans that were absent from the original record.

## Phase 3 evaluation cases

Legacy suite rows remain accepted:

```json
{"id":"Q-1","question":"When?","expect":"answer","must_contain":["October"]}
```

They migrate in memory to ordered `messages`, `expected_outcome`, and an
`assertions` list. New datasets should persist the schema 2.0 representation.
`Case` remains a constructor-compatible alias; new code should use
`EvaluationCase` and `load_dataset`. Dataset hashes intentionally exclude
platform-generated message timestamps and IDs, so identical authored content
has the same immutable version.

JSON and YAML documents may contain `cases`, `include`, `parameters`, and
`metadata`. Composition cycles, unknown parameters, unsupported formats, schema
errors, and empty JSONPath matches are explicit failures. Destructive fixtures
require explicit approval and never run by default.

Scoring persists each deterministic assertion result in `assertion_results`,
including a stable failure code and evidence references. Fixture execution
wraps each record or replay case, always cleans up prepared state, and adds
before/after snapshot artifacts and verification evidence to the trace.

For DNS-rebinding resistance, HTTP fixture URLs accept only literal public
IPv4 or IPv6 hosts. DNS names and non-public addresses are rejected. HTTPS is
the default; HTTP requires the explicit `allow_http` fixture option.

# Phase P4 — Framework polish (aws-execution-engine)

## Context

**Why:** P0/P1/P2/P3 landed and the engine now has a stable architecture (P2) and bounded runtime (P3). Four "framework polish" items remain from the gap-fix plan. Each addresses a quality-of-life or maintainability issue that isn't runtime-critical but prevents the engine from being a clean library consumers can upgrade against. Pipeline #38 is at 475 tests green.

**Four P4 items** (all independent, from `plan-to-fix-gaps-04.09.2026.md:298-330`):

- **P4-1** — EventSink protocol + composite sink. `src/common/dynamodb.py:177`'s `put_event` is the single chokepoint; 7 production call sites write events through it. Introduces a pluggable `EventSink` protocol mirroring the P2 registry pattern (credentials, code_sources, vcs, orchestrator/targets).
- **P4-3** — Versioned result schema. 5 result.json writers + 2 readers. Add `schema_version: "v1"` consistently. Greenfield addition (no version field today). NOT using Pydantic — matches existing `dataclass + DictMixin` pattern in `src/common/models.py`.
- **P4-4** — Contract drift test. Stub already exists at `tests/unit/test_contract_drift.py` (P0-2 planted one test: `test_event_sk_format_matches_code`). Extend with 5 more categories. Research surfaced 3 real drift items in docs that the new guards will catch — doc fixes are included in this phase so all guards ship green.
- **P4-2** — Package rename `src.*` → `aws_exe_sys.*`. **HARD CUT, no compat shim** (per user feedback `feedback_greenfield_no_backwards_compat.md`). 167 Python references + 3 Dockerfiles + 10 Terraform refs + 1 entrypoint.sh + 6 doc refs + dynamic `__import__` in bootstrap_handler. Scheduled last so any rename breakage doesn't block the three additive items.

**Outcome:** Engine is installable as a proper package (`aws_exe_sys`), events are pluggable, results are versioned, and CI catches doc/code drift automatically.

## Phase ordering rationale

**P4-1 → P4-3 → P4-4 → P4-2** (user confirmed via AskUserQuestion).

- P4-1/P4-3/P4-4 are additive. They add code, tests, and docs without moving anything. Risk is local to each item.
- P4-2 is a 167-reference hard-cut refactor. Scheduled last so if it breaks, the three additive items are already committed and CI is already green on them.
- P4-2 will also sweep up the new code from P4-1/P4-3/P4-4 in the same pass — one unified rename, not two.

**Key side-effect**: the worker writes P4-1/P4-3/P4-4 code under `src/` (current layout). P4-2 then moves it to `aws_exe_sys/`. The worker must write `src.common.events.emit(...)` in P4-1, not `aws_exe_sys.common.events.emit(...)`.

---

## P4-1 — EventSink abstraction (5 new tests)

### Design (mirrors P2 `orchestrator/targets/` pattern exactly)

The four existing P2 registries are all shaped the same way:
- `base.py` — Protocol definition (`@runtime_checkable`, instance attributes, method signatures)
- `registry.py` — Global dict `_REGISTRY: Dict[str, T]`, `register_*()`, `get_*()`, `list_*()`, `Unknown*Error`, public alias
- `__init__.py` — Seeds built-ins at import time via `register_*(BuiltinImpl())`

The cleanest model for events is **`orchestrator/targets/`** because:
- Sinks are stateless singletons (like targets), not per-run factories (like code_sources)
- Call sites want one shared dispatch point, not a per-order instance
- `TARGETS` public alias lets `dispatch.py` iterate without importing the private dict — same for `SINKS`

### Files to create

**`src/common/events/base.py`** — new file (~30 lines):

```python
"""EventSink protocol.

Third parties can register additional event sinks (CloudWatch Logs,
Kinesis, Datadog, ...) at import time via ``register_sink``. The
orchestrator and workers emit events through ``events.emit()``, which
fans out to every registered sink.
"""
from __future__ import annotations

from typing import Any, Dict, Protocol, runtime_checkable


@runtime_checkable
class EventSink(Protocol):
    """Protocol for pluggable event sinks."""

    name: str  # e.g. "dynamodb", "cloudwatch", "kinesis"

    def emit(self, event: Dict[str, Any]) -> None:
        """Emit a single event dict. MUST NOT raise for transient errors;
        log and return. Permanent errors may raise."""
        ...
```

**`src/common/events/registry.py`** — new file (~80 lines):

```python
"""EventSink registry."""
from __future__ import annotations

import logging
from typing import Dict, List

from .base import EventSink

logger = logging.getLogger(__name__)


class UnknownSinkError(ValueError):
    """Raised when ``get_sink`` is asked for an unregistered sink name."""


_SINKS: Dict[str, EventSink] = {}


def register_sink(sink: EventSink, *, name: str = "") -> None:
    resolved = name or getattr(sink, "name", "") or ""
    if not resolved:
        raise ValueError(
            "event sink name must be a non-empty string; "
            "set the `name` class attribute or pass name=..."
        )
    _SINKS[resolved] = sink


def get_sink(name: str) -> EventSink:
    if name not in _SINKS:
        raise UnknownSinkError(
            f"unknown event sink {name!r}; "
            f"registered sinks: {sorted(_SINKS)}"
        )
    return _SINKS[name]


def list_sinks() -> List[str]:
    return list(_SINKS.keys())


SINKS = _SINKS  # public alias, matches `orchestrator.targets.TARGETS`
```

**`src/common/events/dynamodb_sink.py`** — new file (~60 lines). Wraps the existing `put_event` logic so the legacy helper stays available but the sink protocol is the new public surface:

```python
"""Built-in DynamoDB event sink.

Wraps the existing ``dynamodb.put_event`` helper so call sites that
go through ``events.emit()`` land in the same table using the same
retry decorator. Migration is incremental — ``put_event`` stays as
the sink's implementation detail and its tests stay valid.
"""
from __future__ import annotations

from typing import Any, Dict

from src.common import dynamodb


class DynamoDbEventSink:
    name = "dynamodb"

    def emit(self, event: Dict[str, Any]) -> None:
        # Extract the three required positional args and let put_event
        # do its existing work. Remaining fields become extra_fields.
        trace_id = event["trace_id"]
        order_name = event["order_name"]
        event_type = event["event_type"]
        status = event["status"]
        data = event.get("data")
        extra = {
            k: v for k, v in event.items()
            if k not in {"trace_id", "order_name", "event_type", "status", "data"}
        }
        dynamodb.put_event(
            trace_id=trace_id,
            order_name=order_name,
            event_type=event_type,
            status=status,
            data=data,
            extra_fields=extra or None,
        )
```

**`src/common/events/composite.py`** — new file (~40 lines):

```python
"""Composite event sink — mirrors to all child sinks, logs failures."""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from .base import EventSink

logger = logging.getLogger(__name__)


class CompositeEventSink:
    """Fan-out sink that emits to every child in registration order.

    Semantics match Python's ``logging.handlers`` behaviour: one child
    failing MUST NOT prevent the other children from being invoked.
    Failures are logged via ``logger.exception`` and swallowed so the
    calling code never sees a sink error.
    """

    def __init__(self, children: List[EventSink], name: str = "composite") -> None:
        self.name = name
        self._children = list(children)

    def emit(self, event: Dict[str, Any]) -> None:
        for child in self._children:
            try:
                child.emit(event)
            except Exception:
                logger.exception(
                    "EventSink child %r raised on emit; continuing to remaining sinks",
                    getattr(child, "name", child),
                )
```

**`src/common/events/__init__.py`** — new file (~40 lines). Seeds the built-in `dynamodb` sink at import time and exposes `emit()` as the package-level dispatch entry point:

```python
"""Event sink package.

Built-in sinks:
    dynamodb — wraps legacy ``put_event`` (default)

Third parties register additional sinks by calling
``register_sink(MySink())`` at import time.

Emission: call ``events.emit(event_dict)`` from anywhere. The event is
dispatched to every sink named in ``AWS_EXE_SYS_EVENT_SINKS`` (comma-
separated, default ``"dynamodb"``).
"""
from __future__ import annotations

import os
from typing import Any, Dict

from .base import EventSink
from .composite import CompositeEventSink
from .dynamodb_sink import DynamoDbEventSink
from .registry import (
    SINKS,
    UnknownSinkError,
    get_sink,
    list_sinks,
    register_sink,
)

# Seed built-ins at import time.
register_sink(DynamoDbEventSink())


def emit(event: Dict[str, Any]) -> None:
    """Top-level emit. Resolves active sinks via ``AWS_EXE_SYS_EVENT_SINKS``
    (comma-separated names) and fans out through a composite."""
    names = os.environ.get("AWS_EXE_SYS_EVENT_SINKS", "dynamodb").split(",")
    names = [n.strip() for n in names if n.strip()]
    children = [get_sink(n) for n in names]
    CompositeEventSink(children).emit(event)


__all__ = [
    "EventSink",
    "DynamoDbEventSink",
    "CompositeEventSink",
    "SINKS",
    "UnknownSinkError",
    "emit",
    "get_sink",
    "list_sinks",
    "register_sink",
]
```

### Files to modify

**None of the production call sites are rewritten in P4-1.** The 7 production `put_event` call sites continue to call `dynamodb.put_event(...)` directly. The sink protocol is introduced as the new public surface but migration of call sites is deferred — `put_event` is the implementation that the built-in `DynamoDbEventSink` wraps, so behavior is identical.

**Rationale**: rewriting 7 call sites to build a dict and call `events.emit()` is noisy churn that doesn't ship a new feature. The value of P4-1 is establishing the protocol + registry so third-party sinks can be plugged in. Migration of the call sites is a separate chore that can be done in P5 or on demand. The CompositeSink and the env-var dispatch prove the protocol is usable. The new test `test_third_party_cloudwatch_stub_registration` proves the extension point works.

### Tests (`tests/unit/test_event_sinks.py` — new file, 5 tests)

- `test_dynamodb_sink_default` — `events.emit({"trace_id": "t1", "order_name": "o1", "event_type": "e1", "status": "running", "flow_id": "f1"})` with moto DynamoDB, assert the row lands in `order_events` table with the expected PK/SK/flow_id.
- `test_composite_sink_mirrors` — construct a `CompositeEventSink([fake_a, fake_b])`, emit one event, assert both fakes' `emit` was called exactly once with the same dict.
- `test_composite_sink_swallows_child_failure` — fake_a raises `RuntimeError`, fake_b is a spy. Emit one event. Assert fake_b was still called (the critical backstop invariant) and no exception propagated.
- `test_third_party_cloudwatch_stub_registration` — register a stub `CloudWatchSink` via `register_sink(CloudWatchSink())`, set `AWS_EXE_SYS_EVENT_SINKS="dynamodb,cloudwatch"`, emit one event, assert both DynamoDB row exists AND the stub was called.
- `test_unknown_sink_raises` — `AWS_EXE_SYS_EVENT_SINKS="dynamodb,nonexistent"`, calling `emit()` raises `UnknownSinkError` with message naming the missing sink.

---

## P4-3 — Versioned result schema (5 new tests)

### Design

Current result.json is `{"status": "...", "log": "..."}` — no version field. Greenfield, no readers do strict validation (both use `.get("status")` / `.get("log")`), so adding a required field is non-breaking.

**Use a dataclass, not Pydantic** — matches the existing `OrderEvent`, `Order`, `Job`, `OrderRecord` patterns in `src/common/models.py`. Pydantic is not currently a dependency (`requirements.txt` only has `boto3>=1.34.0` and `requests>=2.31.0`), and adding it for one dataclass is disproportionate.

### Files to create

**`src/common/schemas.py`** — new file (~60 lines):

```python
"""Versioned result schemas for worker/watchdog/init result.json payloads.

``v1`` is the current format. Future versions add a new class and bump
``SCHEMA_VERSION_CURRENT``. Readers dispatch on the schema_version field.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

SCHEMA_VERSION_CURRENT = "v1"


@dataclass
class ResultV1:
    """result.json schema v1 — worker / watchdog / init writers."""

    status: str
    log: str
    schema_version: str = SCHEMA_VERSION_CURRENT

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResultV1":
        version = data.get("schema_version")
        if version != SCHEMA_VERSION_CURRENT:
            raise ValueError(
                f"expected result schema_version={SCHEMA_VERSION_CURRENT!r}, "
                f"got {version!r} — caller must upgrade or migrate"
            )
        return cls(
            status=data["status"],
            log=data["log"],
            schema_version=version,
        )
```

### Files to modify

**`src/common/s3.py`** — 3 writer call sites:

1. `write_result` (around line 67-80) — change from `json.dumps({"status": status, "log": log})` to `json.dumps(ResultV1(status=status, log=log).to_dict())`. Import `ResultV1` at top of file.
2. `write_init_trigger` (around line 83-93) — same change, `ResultV1(status="init", log="")`.
3. `read_result` (around line 50-64) — change from `return json.loads(body)` to `return ResultV1.from_dict(json.loads(body))`. Then update the downstream caller in `orchestrator/read_state.py` to access `.status` and `.log` as dataclass attributes instead of `.get("status")` / `.get("log")`.

**`src/worker/callback.py`** — `send_callback` (around line 35): change `payload = json.dumps({"status": status, "log": log})` to `payload = json.dumps(ResultV1(status=status, log=log).to_dict())`. Import at top.

**`src/orchestrator/read_state.py`** — the caller of `read_result`. Today it calls `result.get("status", FAILED)` / `result.get("log", "")`. After the change, `read_result` returns a `ResultV1` instance; access `result.status` and `result.log` as attributes. The `get(..., default)` calls go away because `from_dict` now enforces presence.

### Tests (`tests/unit/test_schemas.py` — new file, 5 tests)

- `test_result_v1_round_trip` — build a `ResultV1(status="succeeded", log="ok")`, `.to_dict()`, `json.dumps`, `json.loads`, `ResultV1.from_dict(...)`, assert equality.
- `test_result_v1_includes_schema_version` — `to_dict()` contains `"schema_version": "v1"`.
- `test_result_from_dict_rejects_missing_version` — `ResultV1.from_dict({"status": "ok", "log": "x"})` raises `ValueError` mentioning `v1`.
- `test_result_from_dict_rejects_wrong_version` — `ResultV1.from_dict({"status": "ok", "log": "x", "schema_version": "v2"})` raises `ValueError`.
- `test_write_result_includes_schema_version` — integration-ish: call `s3_ops.write_result(...)` against moto S3, fetch the object, assert `json.loads(body)["schema_version"] == "v1"`.

Also verify that existing tests that assert on result body shape (e.g., tests for `write_result`, `send_callback`, watchdog tests) still pass — the added field is non-breaking.

---

## P4-4 — Contract drift test (6 new tests + doc fixes)

### Design

Extend the existing stub at `tests/unit/test_contract_drift.py` with guards for the 5 remaining gap-plan categories. Fix the 3 actual drift items found during research before the guards ship, so CI stays green.

### Doc fixes (MUST go in the same run as the drift tests)

**`CLAUDE.md`** — add the missing env vars to the "Environment Variables" section (around lines 44-54):

- `AWS_EXE_SYS_SSM_DOCUMENT` — SSM document name (used by orchestrator for SSM Run Command dispatch)
- `AWS_EXE_SYS_EVENT_TTL_SECONDS` — order_events TTL in seconds, default `86400 * 90` (90 days)
- `AWS_EXE_SYS_SSM_PREFIX` — SSM Parameter Store path prefix for SOPS keys, default `exe-sys`
- `AWS_EXE_SYS_EVENT_SINKS` — comma-separated sink names, default `dynamodb` (NEW in P4-1)

**`CLAUDE.md`** — add explicit SSM SOPS path format to the "SOPS key persistence" section (around line 61):

> Private key path format: `/{AWS_EXE_SYS_SSM_PREFIX}/sops-keys/{run_id}/{order_num}` (default prefix `exe-sys`).

**`CLAUDE.md`** — clarify Lock TTL (around line 74) from `"TTL: max_timeout"` to the actual default value `3600` (1 hour) with note that it's overridable by `acquire_lock(ttl=...)`.

### Files to modify

**`tests/unit/test_contract_drift.py`** — extend the existing file. The existing `test_event_sk_format_matches_code` at the top stays. Add at the bottom:

```python
# ---------------------------------------------------------------------------
# New P4-4 drift guards
# ---------------------------------------------------------------------------

import pytest
from src.common import dynamodb, sops
from src.common.statuses import EXECUTION_TARGETS
from src.orchestrator import lock

CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ARCHITECTURE_MD = REPO_ROOT / "docs" / "ARCHITECTURE.md"
VARIABLES_MD = REPO_ROOT / "docs" / "VARIABLES.md"


def _read(path):
    return path.read_text()


def test_event_ttl_value_matches_docs():
    """CLAUDE.md and ARCHITECTURE.md both claim 90-day TTL; code default matches."""
    # Code: dynamodb.py has `ttl_seconds = int(os.environ.get("AWS_EXE_SYS_EVENT_TTL_SECONDS", 86400 * 90))`
    claude_text = _read(CLAUDE_MD)
    arch_text = _read(ARCHITECTURE_MD)
    assert "90 days" in claude_text, "CLAUDE.md missing '90 days' TTL claim"
    assert "90 days" in arch_text, "docs/ARCHITECTURE.md missing '90 days' TTL claim"
    # Code literal sanity: ensure the 86400*90 appears in dynamodb.py
    code_text = (REPO_ROOT / "src" / "common" / "dynamodb.py").read_text()
    assert "86400 * 90" in code_text, (
        "src/common/dynamodb.py no longer has the 86400*90 literal — "
        "if the default changed, update CLAUDE.md and docs/ARCHITECTURE.md to match"
    )


def test_lock_ttl_default_matches_code():
    """CLAUDE.md documents the lock TTL default; code is acquire_lock(..., ttl=3600)."""
    from inspect import signature
    sig = signature(lock.acquire_lock)
    default_ttl = sig.parameters["ttl"].default
    claude_text = _read(CLAUDE_MD)
    assert str(default_ttl) in claude_text, (
        f"CLAUDE.md does not mention lock TTL default ({default_ttl}s); "
        "update the 'DynamoDB Tables' section to document the actual default"
    )


@pytest.mark.parametrize("env_var", [
    "AWS_EXE_SYS_ORDERS_TABLE",
    "AWS_EXE_SYS_ORDER_EVENTS_TABLE",
    "AWS_EXE_SYS_LOCKS_TABLE",
    "AWS_EXE_SYS_INTERNAL_BUCKET",
    "AWS_EXE_SYS_DONE_BUCKET",
    "AWS_EXE_SYS_WORKER_LAMBDA",
    "AWS_EXE_SYS_CODEBUILD_PROJECT",
    "AWS_EXE_SYS_WATCHDOG_SFN",
    "AWS_EXE_SYS_EVENTS_DIR",
    "AWS_EXE_SYS_SSM_DOCUMENT",       # added P4-4 doc fix
    "AWS_EXE_SYS_EVENT_TTL_SECONDS",  # added P4-4 doc fix
    "AWS_EXE_SYS_SSM_PREFIX",         # added P4-4 doc fix
    "AWS_EXE_SYS_EVENT_SINKS",        # added P4-1 + P4-4 doc fix
])
def test_env_var_documented_in_claude_md(env_var):
    """Every AWS_EXE_SYS_ env var used in code must be documented in CLAUDE.md."""
    claude_text = _read(CLAUDE_MD)
    assert env_var in claude_text, (
        f"Env var {env_var} is used in code but not documented in CLAUDE.md "
        f"(Environment Variables section)"
    )


def test_execution_targets_documented_match_code():
    """CONTRACT.md enumerates execution_target values; must match EXECUTION_TARGETS."""
    contract_text = _read(CONTRACT_MD)
    code_targets = set(EXECUTION_TARGETS)
    for target in code_targets:
        assert f'"{target}"' in contract_text or f"'{target}'" in contract_text, (
            f"Code has execution_target={target!r} but CONTRACT.md doesn't mention it"
        )


def test_ssm_sops_key_path_format_documented():
    """The SSM SOPS key path format is a cross-service contract — must be pinned in docs."""
    code_text = (REPO_ROOT / "src" / "common" / "sops.py").read_text()
    # Code has: path = f"/{prefix}/sops-keys/{run_id}/{order_num}"
    assert 'sops-keys/{run_id}/{order_num}' in code_text, (
        "sops.py path format changed — update CLAUDE.md to match"
    )
    claude_text = _read(CLAUDE_MD)
    assert "sops-keys/{run_id}/{order_num}" in claude_text, (
        "CLAUDE.md must document the exact SSM SOPS key path format because "
        "consumers rely on it for out-of-band key management"
    )
```

### Tests summary (P4-4)

Total new test cases: **6** (1 function + 1 parametrize × 13 env vars expands to 13 test IDs, but counted per-function in the plan).

By test function:
1. `test_event_ttl_value_matches_docs`
2. `test_lock_ttl_default_matches_code`
3. `test_env_var_documented_in_claude_md` (parametrized across 13 env vars)
4. `test_execution_targets_documented_match_code`
5. `test_ssm_sops_key_path_format_documented`
6. (Existing `test_event_sk_format_matches_code` stays at the top — not counted as "new")

Pytest will show the parametrized test as 13 separate cases in the output, so the CI log will display ~17 new drift-related PASSED lines. The 15-test-per-P-item pattern from P3 doesn't quite apply here — P4-4 is inherently about category count, not test count.

---

## P4-2 — Package rename (hard cut)

### Scope

**167 Python references + 3 Dockerfiles + 10 Terraform refs + 1 entrypoint.sh + 6 doc refs + `bootstrap_handler.py` dynamic `__import__`.**

Research (research agent) enumerated every file. Blast radius:

- **Python source**: 27 files under `src/` (common: 8, init_job: 5, orchestrator: 6, ssm_config: 4, watchdog_check: 1, worker: 3)
- **Python tests**: 44 files under `tests/unit/` and `tests/integration/`
- **Dockerfiles**: `docker/Dockerfile`, `docker/Dockerfile.test`, `docker/Dockerfile.base` — 6 references (COPY + CMD)
- **Terraform**: `infra/02-deploy/lambdas.tf` — 10 references (5 Lambdas × 2 per function: `image_config.command` ternary + `ENGINE_HANDLER` env var conditional)
- **Shell**: `src/worker/entrypoint.sh` line 5 — `python -m src.worker.run`
- **Dynamic import**: `src/bootstrap_handler.py:191` — default `ENGINE_HANDLER` string `"src.init_job.handler"`
- **Docs**: `docs/REPO_STRUCTURE.md` — 6 references

### Mechanical steps

1. **`git mv src aws_exe_sys`** — preserves history. (Git will detect the rename per-file automatically.)

2. **Create `pyproject.toml`** at the engine root (new file):
   ```toml
   [project]
   name = "aws_exe_sys"
   version = "0.1.0"
   description = "Generic event-driven continuous delivery system for IaC and arbitrary command execution"
   requires-python = ">=3.14"
   dependencies = [
       "boto3>=1.34.0",
       "requests>=2.31.0",
   ]

   [project.optional-dependencies]
   test = [
       "pytest>=8.0.0",
       "moto[all]>=5.0.0",
   ]

   [build-system]
   requires = ["setuptools>=68"]
   build-backend = "setuptools.build_meta"

   [tool.setuptools.packages.find]
   include = ["aws_exe_sys*"]
   ```

3. **Find/replace Python imports** across `aws_exe_sys/**/*.py` and `tests/**/*.py`:
   - `from src.` → `from aws_exe_sys.`
   - `import src.bootstrap_handler` → `import aws_exe_sys.bootstrap_handler`
   - Any nested / indented `from src.` inside conditional blocks or test fixtures
   - Verify no `import src` (bare) remains

4. **Update Dockerfiles**:
   - `docker/Dockerfile:25` — `COPY src/ ${LAMBDA_TASK_ROOT}/src/` → `COPY aws_exe_sys/ ${LAMBDA_TASK_ROOT}/aws_exe_sys/`
   - `docker/Dockerfile:32` — `CMD ["src.worker.handler.handler"]` → `CMD ["aws_exe_sys.worker.handler.handler"]`
   - `docker/Dockerfile.test:8` — `COPY src/` → `COPY aws_exe_sys/`
   - `docker/Dockerfile.base:18` — `COPY src/bootstrap_handler.py` → `COPY aws_exe_sys/bootstrap_handler.py`
   - `docker/Dockerfile.base:20` — CMD reference (if any) updated

5. **Update `src/worker/entrypoint.sh:5`** (will be `aws_exe_sys/worker/entrypoint.sh` after git mv) — `python -m src.worker.run` → `python -m aws_exe_sys.worker.run`

6. **Update `aws_exe_sys/bootstrap_handler.py:191`** — default `ENGINE_HANDLER` string:
   ```python
   handler_module = os.environ.get("ENGINE_HANDLER", "aws_exe_sys.init_job.handler")
   ```

7. **Update `infra/02-deploy/lambdas.tf`** — all 10 references. Each Lambda function has TWO that need changing: the `image_config.command` ternary and the `ENGINE_HANDLER` env var conditional. Example for `init_job`:
   ```hcl
   image_config {
     command = var.engine_code_source.kind == "inline"
       ? ["aws_exe_sys.init_job.handler.handler"]
       : ["aws_exe_sys.bootstrap_handler.handler"]
   }
   # ...
   variables = merge(
     # ...
     var.engine_code_source.kind == "ssm_url"
       ? { ENGINE_HANDLER = "aws_exe_sys.init_job.handler" }
       : {},
   )
   ```
   Do this for all 5 Lambda functions (`init_job`, `orchestrator`, `watchdog_check`, `worker`, `ssm_config`). Both ternary branches reference `src.*` today — BOTH must be updated in every case.

8. **Update `docs/REPO_STRUCTURE.md`** — 6 references in tables and mermaid diagrams. Purely cosmetic but keeps the P4-4 drift test happy.

### Tests

**No new tests.** P4-2 is a refactor, not a feature. Verification is: **the existing 475 + new P4-1/P4-3/P4-4 tests all still pass after the rename**. Any import that was missed will show up as a collection error in pytest.

Specifically target for regression:
- `tests/unit/test_bootstrap_handler.py` — imports `src.bootstrap_handler` (will be `aws_exe_sys.bootstrap_handler`). This test validates the dynamic `__import__` path.
- `tests/integration/test_worker_events.py:106, 161` — nested conditional imports (easy to miss).

### Verification grep pass

After the rename, run this grep to verify zero `src.` references remain in code:

```bash
grep -rn "from src\.\|import src\.\|\"src\.\|'src\." aws_exe_sys/ tests/ docker/ infra/02-deploy/lambdas.tf docs/
```

Expected output: **zero matches** (or only the explicit historical reference in a docstring comment, if any).

---

## Phase ordering and deploy gate

### Phases (all assigned to backend-worker, one at a time)

1. **P4-1** — EventSink protocol + composite sink + dynamodb wrapper. 5 new tests. 475 → 480.
2. **P4-3** — ResultV1 schema + 5 writer/reader updates. 5 new tests. 480 → 485.
3. **P4-4** — Drift test extensions + CLAUDE.md doc fixes. 6 new test functions (1 parametrized to 13 cases). 485 → 490+ (exact count depends on parametrize expansion). All guards PASSED from day one.
4. **P4-2** — Hard-cut rename. Zero new tests. All 490+ existing tests still PASSED after the rename.

### Deploy gate (library verification, matches P0/P1/P2/P3 pattern)

Per `feedback_library_vs_service_verification.md`: aws-execution-engine is a standalone library. No live AWS. The gate is:

1. Backend-worker runs full unit suite locally after each phase. Reports per-phase counts.
2. After all four phases, worker runs final full suite. Reports "ready for deploy."
3. Team-lead reviews diff for:
   - EventSink registry shape matches P2 targets pattern
   - CompositeSink swallows child exceptions (backstop invariant)
   - ResultV1 is a dataclass (no Pydantic added)
   - P4-4 drift tests cover all 6 gap-plan categories
   - CLAUDE.md has the 4 doc additions (3 missing env vars + SSM path format + Lock TTL clarification + P4-1 env var)
   - P4-2 zero `from src.` references remain (grep verification)
   - `infra/02-deploy/lambdas.tf` has all 10 references updated (5 × 2)
   - `bootstrap_handler.py` default ENGINE_HANDLER string updated
4. Deployer independently reruns full unit suite. Confirms 490+ passed.
5. **One monolithic commit**: `feat(aws-exe-sys): P4 framework polish (EventSink, result schema v1, drift tests, aws_exe_sys package rename)`. No `--no-verify`, no amend.
6. Deployer runs `tools/sync-forgejo.sh aws-execution-engine`.
7. Deployer finds Woodpecker pipeline #39, waits, reads full log.
8. Deployer confirms all NEW tests from P4-1/P4-3/P4-4 pass by name:
   - 5 P4-1 tests: `test_dynamodb_sink_default`, `test_composite_sink_mirrors`, `test_composite_sink_swallows_child_failure`, `test_third_party_cloudwatch_stub_registration`, `test_unknown_sink_raises`
   - 5 P4-3 tests: `test_result_v1_round_trip`, `test_result_v1_includes_schema_version`, `test_result_from_dict_rejects_missing_version`, `test_result_from_dict_rejects_wrong_version`, `test_write_result_includes_schema_version`
   - 6 P4-4 tests: `test_event_sk_format_matches_code` (pre-existing, confirm still green), `test_event_ttl_value_matches_docs`, `test_lock_ttl_default_matches_code`, `test_env_var_documented_in_claude_md[*]` (13 parametrized cases), `test_execution_targets_documented_match_code`, `test_ssm_sops_key_path_format_documented`
9. Deployer reports: local SHA, Forgejo SHA, pipeline #, URL, pytest summary line, 16+ test excerpts.
10. Results file written at `src/delegated-execution-components/aws-execution-engine/results/p4/results.md`.
11. Active marker `.state/claude/team-implement-active` removed.

### Optional validation

- `cd infra/02-deploy && tofu validate` after P4-2 Terraform changes. Same as P3 — skip if terraform binary isn't in the worker's sandbox.
- `python -c "from aws_exe_sys.orchestrator.handler import handler"` as a sanity check that the rename didn't miss any package-level import.

---

## Test count expectation

| Item | New tests | Cumulative |
|---|---|---|
| Baseline (post-P3) | — | 475 |
| P4-1 | 5 | 480 |
| P4-3 | 5 | 485 |
| P4-4 | 5 new functions + 13 parametrized cases = 18 test IDs | ~503 |
| P4-2 | 0 | ~503 |
| **Total** | **~28** | **~503 passing** |

Exact post-P4-4 count depends on pytest parametrize expansion. Worker reports the true number at verification time.

## Out of scope (explicitly)

- **Migrating the 7 `put_event` call sites to `events.emit()`** — that's a follow-up chore. P4-1 ships the protocol and proves the extension point; call-site migration is additive and can happen independently.
- **Adding Pydantic** — explicitly rejected. Use `dataclass + DictMixin` to match existing patterns.
- **Live AWS `terraform apply`** — library gate only.
- **GitHub origin push** — P4's commit lives on local `main` and Forgejo only, matching the P0/P1/P2/P3 pattern.
- **Compat shim for `src.*` imports** — explicitly rejected (hard cut, user confirmed).
- **P5 or beyond** — not in this run.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| P4-2 misses an import → Lambda cold-start crash | Grep verification + Woodpecker CI collection catches it before deploy |
| P4-3 readers break on old result.json (e.g. old S3 objects) | Not applicable — greenfield, no production data |
| P4-4 drift test is too strict and blocks future doc rewrites | Mitigated by writing the tests against concrete anchors (specific strings), not generic presence checks. Anyone rewriting docs can update the test in the same PR. |
| P4-1 composite sink swallows a critical error | `logger.exception` at warning level makes failures visible in CloudWatch; the invariant is intentional (matches Python `logging.handlers` behavior) |
| P4-2 breaks `bootstrap_handler.py` dynamic `__import__` | Explicit test `test_bootstrap_handler.py` already exercises the dynamic path; the rename test will catch any miss |
| Phase ordering causes rebase conflicts | P4-1/P4-3/P4-4 are additive and touch distinct files; P4-2 touches ALL files at the end but mechanically — conflicts only if the worker forgets which phase they're in |

## End state

With P4 landed, the engine is:

- **Pluggable** — events, credentials, VCS, code sources, execution targets all go through runtime registries with documented extension points
- **Runtime-bounded** — watchdog hard cap, SOPS TTL coordination, callback fallback (from P3)
- **Versioned** — result.json carries an explicit schema_version for future migrations
- **Installable** — `pip install aws_exe_sys` would work (with the new `pyproject.toml`)
- **Self-policing** — CI fails if anyone drifts the docs from code for the 6 protected categories

After P4 is the logical stopping point for the "framework polish" track. Further work (P5+) would be feature additions: migrating call sites to `events.emit()`, adding sink implementations (CloudWatch, Kinesis), writing a consumer-facing quickstart, publishing to a private PyPI, etc. None of those are planned for this run.

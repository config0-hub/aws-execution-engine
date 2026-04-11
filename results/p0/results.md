# aws-exe-sys — P0 gap fixes (results)

**Date:** 2026-04-10
**Plan:** `src/delegated-execution-components/aws-execution-engine/plan-to-fix-gaps-04.09.2026.md`
**Scope:** Phase P0 only (16 items). Phases P1–P4 deferred to a future run.
**Workflow:** `/team:implement-simple` with 2 workers (backend + docs) and deployer.

## Outcome

**PASS.** All 16 P0 items shipped. 392/392 unit tests green in Woodpecker CI.

## Phase P0 items — status

| ID | Item | Status | Files |
|---|---|---|---|
| P0-1 | Event TTL 15min→90d, configurable via `AWS_EXE_SYS_EVENT_TTL_SECONDS` | done | `src/common/dynamodb.py`, `CLAUDE.md` |
| P0-2 | CONTRACT.md SK format drift | done | `CONTRACT.md` |
| P0-3 | Lock with empty `flow_id`/`trace_id` | done | `src/orchestrator/handler.py` |
| P0-4 | Dispatch-before-status duplicate-dispatch risk | done — two-step `reserve_order_for_dispatch` helper | `src/orchestrator/dispatch.py`, `src/common/dynamodb.py` |
| P0-5 | Cycle detection missing | done — DFS WHITE/GRAY/BLACK with path string | `src/init_job/validate.py` |
| P0-6 | Lock acquire lacks TTL check | done — `Attr("ttl").lt(Decimal(now))` | `src/common/dynamodb.py` |
| P0-7 | `env_dict` plaintext in DynamoDB | done — removed `OrderRecord.env_dict`, both writer + reader | `src/common/models.py`, `src/ssm_config/insert.py`, `src/orchestrator/dispatch.py` |
| P0-8 | Presign-expiry vs order-timeout validation | done | `src/init_job/validate.py` |
| P0-9 | `use_lambda` doc drift | done — removed from docs | `docs/ARCHITECTURE.md`, `docs/VARIABLES.md` |
| P0-10 | Stale `TODO/sops-key-ssm-storage.md` | done — deleted | `TODO/sops-key-ssm-storage.md` |
| P0-11 | Phantom `pr_comment.py` reference | done | `docs/REPO_STRUCTURE.md` |
| P0-12 | PR comment mermaid nodes in docs | done — removed, added library-only note | `docs/ARCHITECTURE.md` |
| P0-13 | `/aws-exe-sys/sops-keys/` path drift | done — code path `/exe-sys/sops-keys/` is authoritative | `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_DIAGRAM.md`, `docs/architecture-diagram.html` |
| A | `fetch_secret_values` JSON-dict expansion | done | `src/common/code_source.py`, `docs/VARIABLES.md` |
| B | `SopsKeyExpired` domain exception + worker finalization | done — `callback_url` threaded via Lambda invoke payload (bundle-independent) | `src/common/sops.py`, `src/worker/run.py`, `src/worker/handler.py`, `src/orchestrator/dispatch.py` |
| C | base64/JSON contract for SSM credential values | done — `ValueError` at boundary + doc contract | `src/common/code_source.py`, `docs/VARIABLES.md` |

## Tests added — 27 new unit tests across 7 new files + 4 expanded files

**New files:** `test_orchestrator_handler.py`, `test_dispatch_idempotency.py`, `test_validate_cycles.py`, `test_lock_acquire.py`, `test_ssm_insert.py`, `test_validate_presign.py`, `test_contract_drift.py`.

**Expanded:** `test_dynamodb.py` (TTL env var), `test_code_source.py` (5 new), `test_sops.py` (SopsKeyExpired), `test_worker_run.py` (sops-key-expired callback).

`test_contract_drift.py` is a doc-code drift guard — it regex-extracts the documented SK format from `CONTRACT.md` and compares it to the literal at `src/common/dynamodb.py:162`. It fails before the P0-2 fix, passes after.

## Deploy evidence

### Local commits (main)

| # | SHA | Message | Diffstat |
|---|---|---|---|
| 1 | `974edcbf` | `chore(aws-exe-sys): rename iac-ci → aws-exe-sys and remove obsolete config0 docs/tests` | 15 files, +39/-1893 |
| 2 | `2dde072b` | `fix(aws-exe-sys): P0 bug fixes and doc drift from RESEARCH3/4 gap analysis` | 29 files, +1289/-244 |

Commit #1 is pre-existing work that was dirty in the working tree at session start (rename campaign + cleanup of obsolete config0 artifacts). Split into its own commit for traceability rather than bundled into P0.

### Pre-deploy check (local)

- Restored `.gitignore` to keep `docker/{age,age-keygen,sops,tofu}` excluded after discovering ~132 MB of untracked Go binaries from an abandoned `Dockerfile.base` experiment. The Woodpecker-path `Dockerfile.test` uses curl-downloaded tools, so excluding them does not break CI.
- Local `docker build -f docker/Dockerfile.test && docker run ... pytest tests/unit/` → **392 passed in 7.09s**.

### Forgejo sync

- `tools/sync-forgejo.sh aws-execution-engine`
- Forgejo commit: `9c1ced98f574` — `sync: update from monorepo 2026-04-10_11:12:34`

### Woodpecker CI

- Repo: `forgejo_admin/aws-execution-engine` (id 28)
- Pipeline: `#35`
- URL: `http://woodpecker/repos/28/pipeline/35`
- Steps: `clone` → `unit-tests` → **success**
- Full log captured at `/tmp/wp-logs-35.txt` (653 lines)
- Zero `ERROR`, `Traceback`, `Exception`, `FAILED` occurrences
- Final line: `============================= 392 passed in 7.12s ==============================`
- All 11 expected new/expanded test files visible in pytest output

### Sample CI passes

```text
tests/unit/test_validate_cycles.py::test_diamond_no_cycle PASSED         [ 80%]
tests/unit/test_validate_presign.py::test_presign_exact_max_plus_buffer PASSED [ 81%]
tests/unit/test_worker_run.py::TestRun::test_callback_failed_on_sops_key_expired PASSED [ 99%]
```

## Notes and deviations

- **No Jenkins trigger for this repo.** Woodpecker is the only CI path. The current Woodpecker pipeline runs unit tests only; there is no live Lambda redeploy step. "Verified live" for this run = Woodpecker pipeline green with all new tests visible in output. A separate pipeline pass (tofu plan/apply) would be needed to redeploy Lambdas with the new code — out of scope for this P0 run and not required for the P0 items to take effect in future deploys.
- **Item B scope expansion:** `callback_url` is now threaded through the Lambda invoke payload (`_dispatch_lambda` → `worker/handler.py` → `run(callback_url=...)`). This is load-bearing — `CALLBACK_URL` previously lived only inside the encrypted SOPS bundle, which means a `SopsKeyExpired` failure left the worker with no URL to callback. Threading it through the invoke payload makes the failure finalization path bundle-independent.
- **P0-6 Decimal wrap:** `Attr("ttl").lt(Decimal(now))` (not `int(now)`) — DynamoDB numeric attributes are Decimals, and comparing Decimal vs int fails at the server.
- **Not committed / deferred to a future session:**
  - Phases P1 (bootstrap seam wiring + SHA integrity) — architectural, needs its own run.
  - Phases P2 (four pluggable registries — credentials, VCS, code sources, execution targets).
  - Phase P3 (runtime hardening — worker callback fallback, watchdog jitter, SOPS TTL coordination).
  - Phase P4 (framework polish — event sinks, installable package, versioned result schema, CI drift test beyond P0-2).
- **Pushed to Forgejo only, not to GitHub origin.** Local commits `974edcbf` and `2dde072b` sit on `main` but have not been pushed to origin. A separate decision/command is needed to push to GitHub.

## Stale marker cleanup

The `.state/claude/team-implement-active` marker contained leftover state from an earlier session (`mkdocs/docs/work-log/add-project-test-run-locally/plan/v3/plan.md` from 2026-04-10 03:14). Overwritten at run start, removed at run end.

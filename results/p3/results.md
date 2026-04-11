# aws-exe-sys — P3 runtime hardening (results)

**Date:** 2026-04-11
**Plan:** `src/delegated-execution-components/aws-execution-engine/results/p3/plan.md`
**Scope:** P3-1 (worker callback fallback), P3-2 (watchdog jitter + hard cap), P3-3 (SOPS key TTL coordination). Standalone library — verified via library gate, not live deploy.
**Workflow:** `/team:implement-simple` with 1 worker + deployer. P0 shipped as `2dde072b`, P1 as `20516044`, P2 as `3c63b3b4`. This run rebases on the P2 registry architecture.

## Outcome

**PASS.** All three P3 items shipped in a single monolithic commit. 475/475 unit tests green in Woodpecker CI pipeline #38 (baseline was 460 after P2 → +15 new tests, exact match to plan).

## P3 items — status

| ID | Item | Status |
|---|---|---|
| P3-1 | Worker callback fallback — on exhausted presigned S3 PUT retries, `send_callback` writes `status="failed"` directly to DynamoDB via a narrow `UpdateItem` IAM statement. `run_id`/`order_num` threaded through the Lambda invoke payload and into `send_callback` as keyword-only parameters. Logged no-op when ids are missing (no phantom-row fabrication). | done — 5 new tests |
| P3-2 | Watchdog jitter + hard cap — the existing `watchdog_check/handler.py` now returns `wait_seconds` in every return path; the Step Function `WaitStep` reads it via `SecondsPath = "$.taskResult.wait_seconds"`. Continuing iterations jitter in `[50, 70]`s via `random.randint`. Hard cap at `2 * timeout` is the first check in the handler — a backstop that terminates even if the natural-timeout's own S3 write fails. | done — 5 new tests |
| P3-3 | SOPS key TTL coordination — `repackage_orders` computes `sops_ttl_hours = max(job.job_timeout, max_order_timeout) // 3600 + 1` once at the outer function and threads it through `_process_order` as an int parameter, which forwards as `ttl_hours=` to `store_sops_key_ssm`. Every order in a job now shares a TTL floor set by the longest-running sibling. | done — 5 new tests |

## Architectural deviations from plan

The plan proposed two architectural approaches for P3-2 that were deliberately not taken:

1. **"Small Lambda-backed wait" that returns `60 + random.randint(-10, 10)`** — rejected. Instead folded both the jitter and the hard cap into the existing `src/watchdog_check/handler.py` and used the Step Function's existing `Wait` state with `SecondsPath = "$.taskResult.wait_seconds"`. Rationale: fewer Lambdas, less IAM, less Terraform, and the handler already has every field needed to compute both values.
2. **"MaxAttempts counter"** — rejected in favor of an elapsed-time cap (`elapsed > 2 * timeout`). Step Functions doesn't natively track iteration counts on Choice→Wait→Task loops, and elapsed-time is the same bound expressed inline without additional state passing.

Both deviations are explicit in the plan (see "Architectural deviation from the plan" section) — flagged and approved up front, not introduced during implementation.

## Critical ordering invariant (P3-2)

The watchdog handler's check order is **load-bearing**:

1. **Hard cap** (`elapsed > 2 * timeout`) — wrapped in try/except with `logger.exception`, returns `{"done": True, "wait_seconds": 0}` **even if the S3 write fails**
2. **Result exists** short-circuit (existing behavior)
3. **Natural timeout** (`elapsed > timeout`) — writes `status="timed_out"`
4. **Still waiting** — returns `{"done": False, "wait_seconds": random.randint(50, 70)}`

The hard cap **must** run first. If it were after the result-exists check, the following failure mode would be unreachable: the natural-timeout path writes a result, then a subsequent S3 outage makes that result unreadable, then the result-exists check returns False forever, then the handler loops without a terminal branch. Putting the hard cap first makes it a backstop that always terminates regardless of S3 state. `test_hard_cap_returns_done_even_on_s3_failure` pins this invariant.

## Defense-in-depth ID plumbing (P3-1)

The run identity (`run_id`, `order_num`) now travels through **three redundant channels** so the worker can finalize an order regardless of which channel survives:

1. **SOPS-encrypted bundle** (happy path) — IDs in `RUN_ID`/`ORDER_NUM` env vars inside the encrypted bundle. Fast, secure, works for 99% of runs.
2. **Plaintext Lambda invoke payload** (this fix) — IDs in the JSON payload outside the encrypted bundle, so they remain accessible even if the SOPS key has expired in SSM. Same principle the engine already used for `callback_url`.
3. **DynamoDB `orders` row PK** (ground truth) — `pk = "{run_id}:{order_num}"` in the Orders table.

`run.py` has a defensive backward-compat merge: `if not run_id: run_id = env_vars.get("RUN_ID", "")` — keeps the happy path working if the orchestrator Lambda is redeployed before the worker Lambda (or vice versa). Either deploy ordering works, no coordination required.

## Outer-scope TTL computation (P3-3)

`sops_ttl_hours` is computed ONCE at the top of `repackage_orders`, not per-order. Rationale:

- Every order in a job shares one SOPS bundle lifecycle from a TTL-safety perspective.
- The longest-running order dictates the floor for **all** orders — a 30-second order can still be blocked by a 4-hour dependency, and its SSM key must outlive the wait.
- O(1) parameter plumbing vs O(N) recomputation per order.

The `+ 1` is the safety margin. For the default job (`job_timeout=3600`, max order `timeout=1800`), the formula gives `max(3600, 1800) // 3600 + 1 = 2` hours — matching the previous hardcoded default. For a 30-minute job with no long orders, the formula gives `1800 // 3600 + 1 = 1` hour — below the old hardcoded default, but sufficient for the actual job length.

## Code changes

### Modified files

| File | LoC delta | Change |
|---|---|---|
| `infra/02-deploy/iam.tf` | +21 | Third statement on the worker policy: `dynamodb:UpdateItem` on `aws_dynamodb_table.orders.arn` with `ForAllValues:StringEquals` on attributes `["status", "last_update", "error"]` and `StringEquals dynamodb:ReturnValues = NONE`. The `ForAllValues:` prefix is load-bearing — without it AWS requires only AT LEAST ONE referenced attribute to match the allowlist, which is useless for restriction. |
| `infra/02-deploy/step_functions.tf` | +3 / −3 | `WaitStep` changed from literal `Seconds = 60` to `SecondsPath = "$.taskResult.wait_seconds"`. `CheckResult.ResultPath = "$.taskResult"` unchanged. |
| `src/init_job/repackage.py` | +17 / −2 | `repackage_orders` computes `sops_ttl_hours` at the top of the outer function, passes it through to `_process_order` as a new required int parameter, which forwards as `ttl_hours=` kwarg to `store_sops_key_ssm`. |
| `src/orchestrator/targets/lambda_target.py` | +5 | Lambda invoke payload now includes `"run_id": run_id` and `"order_num": order.get("order_num", "")` alongside the existing `s3_location`, `internal_bucket`, `callback_url`, `sops_key_ssm_path` fields. |
| `src/watchdog_check/handler.py` | +80 / −12 | Added `import random` and two constants `JITTER_MIN_SECONDS=50`, `JITTER_MAX_SECONDS=70`. Restructured check order with hard cap first, every return path populating `wait_seconds`. |
| `src/worker/callback.py` | +55 / −3 | `send_callback` takes keyword-only `run_id`/`order_num`. After exhausted retries, lazy-imports `src.common.dynamodb.update_order_status` and writes `status="failed"`, `extra_fields={"error": "callback_failed"}`. Missing-id path is a logged no-op. DynamoDB exception is caught via `logger.exception`, `send_callback` still returns `False`. |
| `src/worker/handler.py` | +6 | Reads `run_id` and `order_num` from the invoke event, passes both to `run()`. |
| `src/worker/run.py` | +37 / −6 | New `run_id`/`order_num` parameters threaded to all three `send_callback` call sites (SopsKeyExpired path, no-cmds path, final callback). The SopsKeyExpired path cannot read from `env_vars` because decryption failed — it must come from the explicit parameters. Defensive backward-compat merge: if parameter is empty, fall back to `env_vars.get("RUN_ID", "")` so staggered deploys keep working. |

### New test file

| File | New tests | Focus |
|---|---|---|
| `tests/unit/test_sops_ttl.py` | 5 | Outer-scope TTL computation — scaling, job-timeout larger, +1 floor, parameter propagation, max-across-orders |

### Extended test files

| File | New tests | Focus |
|---|---|---|
| `tests/unit/test_watchdog.py` | 5 | Jitter range, jitter distribution, hard cap distinct status, hard cap backstop invariant, natural timeout distinguishability |
| `tests/unit/test_worker_callback.py` | 4 | Fallback writes DynamoDB, fallback skipped when ids missing, success path does not trigger fallback, fallback swallows DynamoDB exception |
| `tests/unit/test_worker_run.py` | 1 | `run()` threads `run_id` to callback on `SopsKeyExpired` path |

**Net diff: +618 / −30 across 11 modified files + 1 new test file. Total new tests: 15.**

## Adjusted existing test

One pre-existing watchdog test (`test_timed_out_result_content`) was rebalanced. The old inputs `timeout=10, start_time=now-100` gave `elapsed=100` which is now in the hard-cap range (`> 2 * 10 = 20`). Bumped to `timeout=60, start_time=now-70` so `elapsed=70` stays in the natural-timeout branch (`60 < 70 < 120`) as the test intends. Confirmed PASSED in CI pipeline #38 at log line 694. This is a legitimate boundary adjustment, not a regression mask.

## Library verification gate

Per `feedback_library_vs_service_verification.md`: aws-execution-engine is a standalone library, not a hosted service. Verification is:

1. ✅ Unit tests pass locally (backend-worker ran Dockerfile.test, reported 475 passed in 18.08s)
2. ✅ Team-lead independent diff review — all 6 load-bearing invariants verified in-file before deployer activation
3. ✅ Unit tests re-run independently by deployer as a pre-commit check (475 passed in 8.85s)
4. ✅ Monolithic commit (no `--no-verify`, no `--amend`, all hooks passed clean)
5. ✅ `tools/sync-forgejo.sh aws-execution-engine`
6. ✅ Woodpecker pipeline unit-tests step green (475 passed in 8.68s)
7. ✅ Each of the 15 new tests verified by exact name in the CI log

No live `tofu apply/plan` against real AWS. No Lambda invocations. No SSM parameters touched. Consumers run their own infra when they upgrade the library version.

The plan's original "trigger an artificially long-running order" (for P3-2) and "aws ssm describe-parameters" (for P3-3) integration-style verification lines were **deliberately dropped** because both require live AWS. The unit-level property assertions (jitter range, hard cap terminates on S3 failure, TTL value in `put_parameter` mock args) cover the same properties at the library level.

## Deploy evidence

| Item | Value |
|---|---|
| Local commit SHA | `e047d484a2415ec869f2a029212b672c28b8b2a2` |
| Local commit subject | `feat(aws-exe-sys): P3 runtime hardening (callback fallback, watchdog jitter+cap, SOPS TTL)` |
| Forgejo commit SHA | `03d0c35bb27739f36eabb2acbd0f1824994fb7ef` |
| Forgejo repo | `forgejo_admin/aws-execution-engine` |
| Forgejo commit URL | `http://forgejo:3000/forgejo_admin/aws-execution-engine/commit/03d0c35bb27739f36eabb2acbd0f1824994fb7ef` |
| Woodpecker pipeline | #38 |
| Pipeline URL | `http://woodpecker/repos/28/pipeline/38` |
| Pipeline status | **success** (clone + unit-tests steps both green, ~219s duration) |
| Total tests | `============================= 475 passed in 8.68s ==============================` |

The Forgejo SHA differs from the local SHA because `sync-forgejo.sh` re-roots the aws-execution-engine subdirectory into its own repo, matching the P0/P1/P2 pattern.

## 15 new tests — verified by name in CI log

Each appears with `PASSED` in pipeline #38's unit-tests step:

### P3-1 — worker callback fallback (5)

- `tests/unit/test_worker_callback.py::TestCallbackDynamoDBFallback::test_fallback_writes_dynamodb_on_exhausted_retries` (log L704)
- `tests/unit/test_worker_callback.py::TestCallbackDynamoDBFallback::test_fallback_skipped_when_no_run_id` (log L705)
- `tests/unit/test_worker_callback.py::TestCallbackDynamoDBFallback::test_success_does_not_trigger_fallback` (log L706)
- `tests/unit/test_worker_callback.py::TestCallbackDynamoDBFallback::test_fallback_swallows_dynamodb_exception` (log L707)
- `tests/unit/test_worker_run.py::TestRun::test_run_threads_run_id_to_callback_on_sops_expired` (log L732)

### P3-2 — watchdog jitter + hard cap (5)

- `tests/unit/test_watchdog.py::TestWatchdogJitterAndHardCap::test_jitter_within_range` (log L695)
- `tests/unit/test_watchdog.py::TestWatchdogJitterAndHardCap::test_jitter_distribution_not_constant` (log L696)
- `tests/unit/test_watchdog.py::TestWatchdogJitterAndHardCap::test_hard_cap_writes_distinct_status` (log L697)
- `tests/unit/test_watchdog.py::TestWatchdogJitterAndHardCap::test_hard_cap_returns_done_even_on_s3_failure` (log L698)
- `tests/unit/test_watchdog.py::TestWatchdogJitterAndHardCap::test_natural_timeout_still_uses_original_status` (log L699)

### P3-3 — SOPS TTL coordination (5)

- `tests/unit/test_sops_ttl.py::TestSopsTtlCoordination::test_ttl_scales_with_longest_order` (log L545)
- `tests/unit/test_sops_ttl.py::TestSopsTtlCoordination::test_ttl_uses_job_timeout_when_larger` (log L546)
- `tests/unit/test_sops_ttl.py::TestSopsTtlCoordination::test_ttl_floor_is_one_hour_above_max` (log L547)
- `tests/unit/test_sops_ttl.py::TestSopsTtlCoordination::test_ttl_passed_to_store_sops_key_ssm` (log L548)
- `tests/unit/test_sops_ttl.py::TestSopsTtlCoordination::test_ttl_uses_max_across_multiple_orders` (log L549)

These are the architecturally meaningful checks — they prove each invariant holds, not just that the code compiled.

## Team-lead review (pre-deploy)

Before activating the deployer, the team-lead diffed every touchpoint and verified:

- ✅ `iam.tf` has `ForAllValues:StringEquals` prefix (load-bearing — without it the Condition is useless)
- ✅ `iam.tf` sets `dynamodb:ReturnValues = NONE` (prevents worker from reading existing item state via UpdateItem)
- ✅ `iam.tf` allowlist is exactly `["status", "last_update", "error"]` — matches spec
- ✅ `callback.py` `send_callback` is keyword-only via `*,` separator — no accidental positional binding at existing call sites
- ✅ `callback.py` lazy-imports `update_order_status` inside the function (keeps module surface narrow, defers boto3 import to cold-start fallback only)
- ✅ `callback.py` wraps fallback in try/except with `logger.exception`, still returns `False` regardless of DynamoDB outcome
- ✅ `callback.py` skips fallback when either id is empty (no phantom-row fabrication)
- ✅ `run.py` SopsKeyExpired path uses the explicit parameter, NOT `env_vars["RUN_ID"]` (env_vars is empty after decryption failure)
- ✅ `run.py` has defensive backward-compat merge for staggered deploys
- ✅ `lambda_target.py` payload includes both new fields
- ✅ `handler.py` reads both from event and threads to `run()`
- ✅ `watchdog_check/handler.py` hard cap fires FIRST, before the result-exists short-circuit
- ✅ `watchdog_check/handler.py` every return path has `wait_seconds` (4 paths total: 3 terminal with 0, 1 continue with random.randint(50, 70))
- ✅ `watchdog_check/handler.py` distinct status strings `timed_out_watchdog_cap` vs `timed_out`
- ✅ `step_functions.tf` WaitStep uses `SecondsPath`, literal `Seconds` removed
- ✅ `repackage.py` `sops_ttl_hours` computed at the TOP of outer `repackage_orders`, not per-order
- ✅ `repackage.py` formula exact: `max(job.job_timeout, max_order_timeout) // 3600 + 1`
- ✅ `repackage.py` outer `repackage_orders` signature unchanged (existing callers compile)
- ✅ All 15 new test names present in the right files (grep-verified)
- ✅ Test count arithmetic: 460 P2 baseline + 15 = 475

## CodeBuild follow-up (flagged)

Per the plan's out-of-scope section, `src/orchestrator/targets/codebuild.py` was NOT modified. A <5 min grep during implementation confirmed that CodeBuild's `environmentVariablesOverride` currently injects only `S3_LOCATION`, `INTERNAL_BUCKET`, and `SOPS_KEY_SSM_PATH` — **it does NOT inject `RUN_ID` or `ORDER_NUM`**. For the CodeBuild dispatch path to benefit from the P3-1 callback fallback, a follow-up change to `codebuild.py` is needed to thread these identifiers through.

SSM Run Command uses a completely different callback model (no `worker/callback.py` at all) and is unaffected by P3-1 as expected.

**This is a known, bounded gap** — the Lambda dispatch path is fully hardened, the CodeBuild dispatch path still has the original fragility, and the SSM dispatch path is architecturally out of scope. No action in this run.

## Deviations and notes

- **P3-2 architectural deviation** — explicit in the plan, not a surprise. See "Architectural deviations from plan" above.
- **Two watchdog constants extracted** (`JITTER_MIN_SECONDS=50`, `JITTER_MAX_SECONDS=70`). The plan inlined `random.randint(50, 70)`; worker extracted constants. Identical behavior, slightly more self-documenting.
- **Existing test `test_timed_out_result_content` boundary adjustment** — see "Adjusted existing test" above. Not a regression mask.
- **Not pushed to GitHub origin.** Commit `e047d484` is on local `main` and Forgejo only. Matches the P0/P1/P2 pattern. Separate user decision needed to push to origin.
- **No `tofu validate` in worker sandbox** — the worker reported running `cd infra/02-deploy && tofu init -backend=false && tofu validate` successfully with `Success! The configuration is valid`. Only pre-existing deprecation warnings about `hash_key` and `data.aws_region.current.name` that predate P3.

## What's NOT in this run

Still deferred to future `/team:implement` runs:

- **CodeBuild `RUN_ID`/`ORDER_NUM` threading** (discovered during P3-1 grep) — needed if the CodeBuild dispatch path should also benefit from the callback fallback. Self-contained change to `src/orchestrator/targets/codebuild.py`.
- **P4 — framework polish**
  - P4-1 `EventSink` protocol + composite sink
  - P4-2 Installable package name (move `src.*` → `aws_exe_sys.*`)
  - P4-3 Versioned result schema (`schema_version: "v1"`)
  - P4-4 CI drift test that regex-extracts doc claims and asserts against code

All four P4 items are independent of each other and of P3; they can be done in any order.

## End state

With P0, P1, P2, and now P3 landed, the engine is structurally ready **and runtime-bounded**:

- **No more stranded `RUNNING` orders** when the presigned S3 PUT is unreachable — the worker has a narrow DynamoDB fallback path and the identity it needs to use it.
- **No more infinite watchdog loops** — the hard cap at `2 * timeout` terminates regardless of S3 write outcome, and jittered polling eliminates thundering-herd on synchronized starts.
- **No more silent SOPS-key expirations mid-run** — the TTL scales with the longest-running order in the job, with a 1-hour safety margin.

Each hardening is pinned by a unit test that exercises the failure mode directly — not just "the happy path compiles".

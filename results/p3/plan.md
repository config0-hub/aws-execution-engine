# Phase P3 — Runtime hardening (aws-execution-engine)

## Context

**Why:** The gap-fix plan at `src/delegated-execution-components/aws-execution-engine/plan-to-fix-gaps-04.09.2026.md` defines five phases (P0–P4) addressing RESEARCH3/4 findings. **P0** (16 doc+bug items) and **P1** (bootstrap seam + SHA256 integrity) and **P2** (four pluggable registries) have all shipped — currently 460 unit tests green in Woodpecker pipeline #37.

**P3 is "runtime hardening"** — three independent items that each close a live fragility in the engine runtime:

- **P3-1** — worker callback fallback: when the worker's presigned S3 PUT fails, the order is stranded forever in `RUNNING` because the worker has no DynamoDB write permission.
- **P3-2** — watchdog jitter + hard cap: the Step Function watchdog polls S3 every 60s with a literal `Seconds: 60` wait and zero iteration bound — thundering-herd risk and no terminal guarantee.
- **P3-3** — SOPS key TTL coordination: `store_sops_key_ssm` defaults to `ttl_hours=2`, no callers override; any order running >2h crashes on SSM fetch.

**Outcome:** Runtime behavior is bounded and recoverable. No more stranded `RUNNING` orders, no more infinite watchdog loops, no more silent SOPS-key expirations mid-run.

## Current state verified after P0/P1/P2 drift

I verified every anchor in the live tree (the plan's line numbers were mostly wrong by a few lines after P0/P1/P2 shifted code around):

| Gap | Status today | Verified anchor |
|---|---|---|
| `callback.py` returns False on exhausted retries, 3 retries | confirmed | `src/worker/callback.py:15-49` (49 LoC total) |
| `send_callback` return value ignored in run.py | confirmed at 3 call sites | `src/worker/run.py:207, 244, 264` |
| Worker has no `UpdateItem` permission | confirmed | `infra/02-deploy/iam.tf:223-247` only grants `PutItem` on `order_events` |
| `update_order_status` helper exists | **confirmed** — reuse, don't rewrite | `src/common/dynamodb.py:142-171` with `@retry_on_throttle` |
| Lambda invoke payload does not thread `run_id`/`order_num` | **confirmed** — NEW fourth touchpoint needed | `src/orchestrator/targets/lambda_target.py:30-37` only has `s3_location`, `internal_bucket`, `callback_url`, `sops_key_ssm_path` |
| Watchdog SFN has literal `Seconds: 60` and no cap | confirmed | `infra/02-deploy/step_functions.tf:26-30` |
| Watchdog handler has `start_time`/`timeout`/`run_id`/`order_num`/`internal_bucket` in event | confirmed | `src/watchdog_check/handler.py:12-62` |
| `random` precedent exists in codebase | confirmed (`random.uniform(0, delay*0.5)` in dynamodb.py throttle backoff) | `src/common/dynamodb.py:44` |
| `store_sops_key_ssm(...ttl_hours: int = 2)` with no overrides | confirmed | `src/common/sops.py:57-92` |
| `SopsKeyExpired` domain exception already exists (P0-B) | confirmed | `src/common/sops.py:14-22` |
| Call site for SOPS key store | confirmed | `src/init_job/repackage.py:59` — both `job` and `order` in scope, `job.orders` accessible |
| `Job.job_timeout: int = 3600`, `Order.timeout: int` required | confirmed | `src/common/models.py` BaseJob and BaseOrder |

**Architectural deviation from the plan (P3-2):** The plan proposes adding a "small Lambda-backed wait" to return `60 + random.randint(-10, 10)`. I am instead folding both the jitter and the hard cap into the **existing** `src/watchdog_check/handler.py`, and using the Step Function's `Wait` state with `SecondsPath: "$.taskResult.wait_seconds"`. Rationale: fewer Lambdas, less IAM, less Terraform, and the handler already has every field needed to compute both values. Also dropping the plan's "MaxAttempts counter" framing in favor of an elapsed-time cap (`now - start_time > 2 * timeout`) — Step Functions doesn't natively track iteration counts on Choice→Wait→Task loops, and elapsed-time is the same bound expressed inline.

---

## P3-1 — Worker callback fallback

### Files to modify

**`src/worker/callback.py`** — make `run_id` and `order_num` keyword-only params on `send_callback`. After the "All callback retries exhausted" log at line 48, add a DynamoDB fallback that imports `update_order_status` lazily and writes `status="failed"`, `extra_fields={"error": "callback_failed"}`. Wrap in try/except so DynamoDB failure doesn't mask the original error — log the exception, still return `False`. Keep the import inside the function (lazy) so the module surface stays narrow (`requests`/`time`/`json`/`logging`) and boto3 is pulled only when fallback actually fires.

**`src/worker/run.py`** — add `run_id: str = ""`, `order_num: str = ""` to `run()` signature. Pass both to all three `send_callback` call sites (lines 207, 244, 264). The SopsKeyExpired path at line 207 CANNOT read them from `env_vars` because decryption has failed; they must come from `run()`'s parameters. The other two call sites can use either `env_vars.get("RUN_ID", "")` (populated by `OrderBundler` at `bundler.py:69`) or the new parameters — prefer the parameters for consistency.

**`src/worker/handler.py`** — read `run_id = event.get("run_id", "")` and `order_num = event.get("order_num", "")` and pass both to `run()`.

**`src/orchestrator/targets/lambda_target.py:30-37`** — add `"run_id": run_id` and `"order_num": order.get("order_num", "")` to the invoke payload. (`run_id` is already a parameter to `dispatch()`; `order_num` lives on the order dict.) This is the **fourth touchpoint** the Plan agent flagged and I verified against the live file.

**Identity-generation principle (user-clarified).** The engine is a library plugged in by other products. Run identity (`run_id`/`order_num`) should always exist by the time dispatch runs — the engine generates them at the top-level entrypoint (`init_job`) if the caller didn't supply them, and they flow through the orchestrator to dispatch naturally. The threading work in P3-1 is therefore mechanical plumbing: both values are already in scope at `lambda_target.py:dispatch`, they just need to land in the payload JSON.

**Fallback no-op semantics.** If for any reason `run_id` or `order_num` is empty at callback-fallback time (upstream caller bug, old-client compat, etc.), the fallback MUST become a logged no-op rather than fabricating synthetic IDs. Writing to DynamoDB with a generated placeholder would hit a row that doesn't match any real order (or worse, create a phantom row). The test `test_fallback_skipped_when_no_run_id` pins this invariant.

**`infra/02-deploy/iam.tf`** — add a third statement to the worker policy:

```hcl
{
  Effect   = "Allow"
  Action   = ["dynamodb:UpdateItem"]
  Resource = aws_dynamodb_table.orders.arn
  Condition = {
    "ForAllValues:StringEquals" = {
      "dynamodb:Attributes" = ["status", "last_update", "error"]
    }
    StringEquals = {
      "dynamodb:ReturnValues" = "NONE"
    }
  }
}
```

`ForAllValues:StringEquals` is load-bearing: without it, AWS only checks that AT LEAST one referenced attribute matches the allowlist, which is useless for restriction. `dynamodb:ReturnValues = NONE` prevents the worker from reading existing item state via UpdateItem's `ReturnValues` parameter. `error` is in the allowlist because the fallback writes it via `extra_fields`.

**P3-1 scope is the Lambda dispatch path only.** The CodeBuild and SSM dispatch paths are out of scope for this phase. Backend-worker should spend <5 minutes grepping `src/orchestrator/targets/codebuild.py` for `RUN_ID`/`ORDER_NUM` env var injection and report what they find — if it's already wired through `environmentVariablesOverride`, no change; if it's missing, note it as a follow-up but do NOT add it in this run. SSM Run Command uses a completely different callback model (no `worker/callback.py` at all) and is not affected by P3-1.

### Tests (new file not required — extend existing)

**`tests/unit/test_worker_callback.py`** (4 new):
- `test_fallback_writes_dynamodb_on_exhausted_retries` — patch `requests.put` to always 500, patch `src.common.dynamodb.update_order_status`, assert called once with `status="failed"`, `extra_fields={"error": "callback_failed"}`.
- `test_fallback_skipped_when_no_run_id` — same 500 path, no `run_id`/`order_num` kwargs, assert `update_order_status` NOT called.
- `test_success_does_not_trigger_fallback` — `requests.put` returns 200, patch `update_order_status`, assert not called.
- `test_fallback_swallows_dynamodb_exception` — `update_order_status` raises `ClientError`, assert `send_callback` returns `False` without propagating.

**`tests/unit/test_worker_run.py`** (1 new):
- `test_run_threads_run_id_to_callback_on_sops_expired` — patch `_decrypt_and_load_env` to raise `SopsKeyExpired`, patch `send_callback`, assert called with `run_id="r1"`, `order_num="0001"`.

**Total P3-1: 5 new tests.**

---

## P3-2 — Watchdog jitter + hard cap

### Files to modify

**`src/watchdog_check/handler.py`** — add `import random`. Restructure the check order so the **hard cap fires first**, before the existing S3-result check and natural timeout check. Reason: the existing S3-result check short-circuits (line 30 returns `done=True` if a `timed_out` result already exists from a prior iteration), so without reordering, once the natural timeout has written its result, subsequent polls never reach the hard cap. The hard cap is a backstop for the case where the natural-timeout's `write_result` itself failed (S3 outage, throttling) — we MUST guarantee the loop terminates regardless.

New flow:

```python
import random
# ...

def handler(event, context=None):
    now = int(time.time())
    start_time = event["start_time"]
    timeout = event["timeout"]
    elapsed = now - start_time

    # (1) Hard cap — backstop, always returns done=True even if write fails
    if elapsed > 2 * timeout:
        try:
            s3_ops.write_result(
                bucket=event["internal_bucket"],
                run_id=event["run_id"],
                order_num=event["order_num"],
                status="timed_out_watchdog_cap",
                log=f"Watchdog hard cap exceeded after {elapsed}s (cap={2*timeout}s)",
            )
        except Exception:
            logger.exception("Hard cap S3 write failed; returning done anyway")
        return {"done": True, "wait_seconds": 0}

    # (2) Happy path — result already exists (existing behavior)
    if check_result_exists(...):
        return {"done": True, "wait_seconds": 0}

    # (3) Natural timeout (existing behavior)
    if elapsed > timeout:
        s3_ops.write_result(..., status="timed_out", log="...")
        return {"done": True, "wait_seconds": 0}

    # (4) Keep polling with jitter
    return {"done": False, "wait_seconds": random.randint(50, 70)}
```

Every return includes `wait_seconds` (0 for terminal returns, `[50, 70]` for the continuing branch) so the SFN's `SecondsPath` always resolves.

**`infra/02-deploy/step_functions.tf:26-30`** — change the `WaitStep`:

```hcl
WaitStep = {
  Type        = "Wait"
  SecondsPath = "$.taskResult.wait_seconds"
  Next        = "CheckResult"
}
```

Only this one state changes. `CheckResult` already sets `ResultPath = "$.taskResult"` so the handler's return JSON is accessible at `$.taskResult.wait_seconds`.

### Tests

**`tests/unit/test_watchdog.py`** (5 new):
- `test_jitter_within_range` — call handler 100 times in the "still waiting" branch (start_time = now, no result), assert every `result["wait_seconds"]` is in `[50, 70]` inclusive.
- `test_jitter_distribution_not_constant` — same 100 calls, assert `len(set(wait_seconds_values)) > 1` to catch the bug where someone hardcodes the value.
- `test_hard_cap_writes_distinct_status` — set `start_time = now - (2 * timeout + 10)`, call handler, assert `result["done"] is True`, fetch the S3 object, assert `body["status"] == "timed_out_watchdog_cap"`.
- `test_hard_cap_returns_done_even_on_s3_failure` — patch `s3_ops.write_result` to raise `ClientError`, set start_time past hard cap, assert `result["done"] is True`. Proves the backstop invariant.
- `test_natural_timeout_still_uses_original_status` — `timeout < elapsed < 2*timeout`, assert `result["done"] is True` and the written S3 body has `status="timed_out"` (not `_watchdog_cap`). Proves distinguishability.

**Total P3-2: 5 new tests.**

---

## P3-3 — SOPS key TTL coordination

### Files to modify

**`src/init_job/repackage.py`** — compute `sops_ttl_hours` ONCE at the top of the outer `repackage_orders` function, pass it down to `_process_order` as a new parameter. Two reasons to compute at the outer level:

1. Every order in the job shares one SOPS bundle lifecycle from a TTL-safety perspective — the longest-running order dictates the floor for ALL orders (a 30-second order can be blocked by a 4-hour dependency, and its SSM key must outlive the wait).
2. Computing once and passing down is O(1) parameter plumbing vs O(N) recomputation per order.

```python
def repackage_orders(job, ...):
    max_order_timeout = max((o.timeout for o in job.orders), default=0)
    sops_ttl_hours = max(job.job_timeout, max_order_timeout) // 3600 + 1
    # ... existing loop, pass sops_ttl_hours into _process_order

def _process_order(job, order, ..., sops_ttl_hours: int):
    # ... at line 59:
    sops_key_ssm_path = store_sops_key_ssm(
        run_id, order_num, private_key_content, ttl_hours=sops_ttl_hours,
    )
```

The `+ 1` is the safety margin — even a 30-minute job gets a minimum 2-hour TTL under `1800 // 3600 + 1 = 1`... wait, that's 1 hour. `(1800 // 3600) + 1 = 0 + 1 = 1`. So the formula gives 1 hour minimum. The plan explicitly says "1-hour safety margin" on top of the computed max, so this is intentional. A job with `job_timeout=3600` and max order `timeout=1800` gets `max(3600, 1800) // 3600 + 1 = 1 + 1 = 2` hours — matches the current default. Fine.

### Tests

**`tests/unit/test_sops_ttl.py`** (new file, 5 tests):
- `test_ttl_scales_with_longest_order` — `Job(job_timeout=3600, orders=[Order(timeout=14400)])` → TTL should be `max(3600, 14400) // 3600 + 1 = 5` hours. Mock `boto3.client("ssm").put_parameter`, assert `Policies` arg contains expiration timestamp ~5 hours in the future (±60s slack).
- `test_ttl_uses_job_timeout_when_larger` — `Job(job_timeout=10800, orders=[Order(timeout=300)])` → TTL = `10800 // 3600 + 1 = 4` hours.
- `test_ttl_floor_is_one_hour_above_max` — proves the `+ 1` safety margin is always applied even for sub-1h timeouts.
- `test_ttl_passed_to_store_sops_key_ssm` — direct unit on `_process_order` with mocked `store_sops_key_ssm`, assert called with `ttl_hours=` matching the computed value.
- `test_ttl_uses_max_across_multiple_orders` — `[Order(timeout=300), Order(timeout=7200), Order(timeout=600)]` → all orders get TTL based on the 7200 max, not their individual timeouts.

**Total P3-3: 5 new tests.**

---

## Independence and sequencing

**All three items are fully independent — zero shared files.**

| Item | Files touched |
|---|---|
| P3-1 | `src/worker/callback.py`, `src/worker/run.py`, `src/worker/handler.py`, `src/orchestrator/targets/lambda_target.py`, `infra/02-deploy/iam.tf`, `tests/unit/test_worker_callback.py`, `tests/unit/test_worker_run.py` (CodeBuild/SSM dispatch paths out of scope) |
| P3-2 | `src/watchdog_check/handler.py`, `infra/02-deploy/step_functions.tf`, `tests/unit/test_watchdog.py` |
| P3-3 | `src/init_job/repackage.py`, `tests/unit/test_sops_ttl.py` (new) |

A single backend worker can do all three in any order. Team composition: same as P2 — **1 backend-worker + 1 deployer**, I am the watchdog.

## Test count expectation

| Item | New tests |
|---|---|
| P3-1 | 5 |
| P3-2 | 5 |
| P3-3 | 5 |
| **Total** | **15** |

**Post-P3 expected: 460 + 15 = 475 unit tests.**

## Deployment gate — library verification

Per `feedback_library_vs_service_verification.md`: aws-execution-engine is a standalone library. No live AWS. The gate is:

1. Backend-worker runs full unit suite locally → all 475 pass → reports "ready for deploy."
2. Team-lead reviews diff for obvious issues (swallowed exceptions, missing `ForAllValues`, SecondsPath typo, etc.).
3. Deployer independently reruns the full unit suite (don't trust worker's count).
4. Deployer commits one monolithic commit: `feat(aws-exe-sys): P3 runtime hardening (callback fallback, watchdog jitter+cap, SOPS TTL)`. No `--no-verify`, no amend.
5. Deployer runs `tools/sync-forgejo.sh aws-execution-engine`.
6. Deployer finds the new Woodpecker pipeline, waits for completion, reads the FULL log.
7. Deployer greps the CI log by name for **all 15 new tests** and confirms each appears with `PASSED`.
8. Deployer reports back with: local commit SHA, Forgejo SHA, pipeline number, pipeline URL, pytest summary line, and excerpts for each of the 15 tests.
9. Results file written at `src/delegated-execution-components/aws-execution-engine/results/p3/results.md` matching the P0/P1/P2 layout.

**Optional but recommended:** `cd infra/02-deploy && terraform validate` in the worker's environment to confirm the new IAM condition and `SecondsPath` change parse cleanly. No `terraform plan` against live AWS. If `terraform` binary isn't in the worker's sandbox, skip — the Woodpecker CI already runs a validate step for Terraform changes (verify this during implementation).

The plan's original "Integration: trigger an artificially long-running order" (for P3-2) and "aws ssm describe-parameters" (for P3-3) verification lines must be **dropped** — both require live AWS. The unit-level property assertions (jitter range, hard cap terminates, TTL value in `put_parameter` mock args) cover the same properties at the library level.

## Out of scope for this run

- **P4 — framework polish:** P4-1 EventSink protocol, P4-2 installable package name (`src.*` → `aws_exe_sys.*`), P4-3 versioned result schema, P4-4 CI drift test. All four P4 items are independent of each other and of P3.
- **Not pushed to GitHub origin.** P3's commit will live on local `main` and Forgejo only, matching the P0/P1/P2 pattern.
- **No live AWS `terraform apply`.** Consumers run their own infra when they upgrade the library version.

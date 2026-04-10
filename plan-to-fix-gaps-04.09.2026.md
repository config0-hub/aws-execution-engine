# Plan to Fix Gaps — 2026-04-09

**Source documents:** [RESEARCH3.md](RESEARCH3.md) (pluggable-framework gap analysis) and [RESEARCH4.md](RESEARCH4.md) (independent verification of RESEARCH3).

**Scope.** This plan consolidates every gap, live bug, and doc-drift finding from RESEARCH3/RESEARCH4 into an ordered, actionable work plan with a concrete verification step for every fix. Phases P0–P4 preserve the ordering recommended by RESEARCH3 §11 and RESEARCH4 §6. Nothing here is speculative — every item has a file:line anchor verified by RESEARCH4.

**Ordering principle.** P0 items are independent live bugs and doc cleanups — do them first and in any order. P1 is the single architectural change (bootstrap seam); every later phase rebases on it. P2 is the pluggable-framework registries. P3 is runtime hardening. P4 is framework polish.

**Verification principle.** Every fix has (a) a unit/integration test that fails before the fix and passes after, plus (b) a targeted manual/smoke check. "Verified" means both. No fix is considered complete if the only evidence is "the code compiles."

---

## Phase P0 — Live bugs and doc drift

Thirteen items. A mix of one-liners, doc fixes, and single-file surgeries. Independent of each other and of the framework question. Tackle in parallel if multiple people are working.

### P0-1 — Event TTL is 15 minutes instead of 90 days

- **Gap.** `src/common/dynamodb.py:170` writes `"ttl": epoch + 900` with inline comment `# 15 min for testing; change to 172800 (2 days) for prod`. `CLAUDE.md:85` and `docs/ARCHITECTURE.md` claim 90-day TTL. Events vanish mid-run.
- **Fix.**
  1. Change `dynamodb.py:170` to `epoch + (86400 * 90)` (90 days, matching `CLAUDE.md`) or make it configurable via an `AWS_EXE_SYS_EVENT_TTL_SECONDS` env var with default 7776000.
  2. Update `CLAUDE.md:85` and `docs/ARCHITECTURE.md` TTL diagram to reflect the final value.
  3. Remove the stale inline comment.
- **Verification.**
  - Unit: add `tests/unit/test_dynamodb_events.py::test_put_event_ttl_is_90_days` that calls `put_event()` with moto, reads the item back, and asserts `item["ttl"] - item["epoch"]` equals the configured value (± 1 s).
  - Smoke: after deploy, write an event via `put_event()` against the real table and confirm `ttl` attribute is ≥ `now + 7776000 - 60`.

### P0-2 — Event SK format drift

- **Gap.** `CONTRACT.md:101` promises `sk = "{order_name}:{epoch}"`. `src/common/dynamodb.py:162` writes `sk = f"{order_name}:{epoch}:{event_type}"`. Consumers writing `begins_with(sk, "order_name:epoch")` queries break.
- **Fix.** Update `CONTRACT.md:101` to the real format: `"{order_name}:{epoch}:{event_type}"`. The `:event_type` suffix is load-bearing — it lets multiple events per second coexist without PK collisions — so update the docs, not the code.
- **Verification.**
  - Unit: add `tests/unit/test_contract_drift.py::test_event_sk_format_matches_code` that reads `CONTRACT.md`, regex-extracts the documented SK format, and compares it to the literal at `dynamodb.py:162`. Fails today, passes after doc update.
  - Manual: `grep -n "order_name.*epoch" CONTRACT.md src/common/dynamodb.py` and eyeball.

### P0-3 — Lock acquired with empty `flow_id` / `trace_id`

- **Gap.** Lock schema at `src/common/dynamodb.py:245-246` stores `flow_id` and `trace_id`, but `src/orchestrator/handler.py:128` literal-passes `flow_id=""`, `trace_id=""`. The fields are wired; the caller never populates them.
- **Fix.** In `orchestrator/handler.py`, move `acquire_lock()` to *after* `read_state()` (or peek at the first order's `flow_id`/`trace_id` before acquiring). One-file surgery.
- **Verification.**
  - Unit: add `tests/unit/test_orchestrator_handler.py::test_lock_stores_flow_trace_ids` that invokes `handler.handler` with a moto DynamoDB fixture containing an order with known `flow_id="f-1"`, `trace_id="t-1"`, then asserts the lock item has those values.
  - Integration: run a full orchestrator cycle against localstack and `aws dynamodb get-item --table locks --key pk=lock:<run_id>`; confirm non-empty `flow_id`.

### P0-4 — Status update happens *after* dispatch (duplicate-dispatch risk)

- **Gap.** `src/orchestrator/dispatch.py:149-154` dispatches, then `:160-169` updates status. If the DynamoDB update fails (throttle, etc.), the orchestrator re-invocation re-dispatches the same order.
- **Fix.** Rewrite the dispatch loop as a two-step:
  1. Conditional `UpdateItem` flipping `status: QUEUED → DISPATCHING` (condition: `status = :queued`).
  2. If condition succeeds, call `_dispatch_<target>`. If it fails, skip (another orchestrator owns it).
  3. After successful dispatch, write `status: DISPATCHING → RUNNING` with `execution_url`.
- **Verification.**
  - Unit: add `tests/unit/test_dispatch_idempotency.py::test_duplicate_dispatch_is_blocked` that simulates two concurrent orchestrator invocations (moto DynamoDB + mocked Lambda client); assert `_dispatch_lambda` is called exactly once.
  - Unit: `test_dispatch_skipped_if_status_not_queued` — seed the order row with `status=RUNNING` and verify dispatch is a no-op.
  - Integration: in the smoke test, force a duplicate S3 callback (two identical `result.json` writes) and confirm only one CodeBuild build is started (`aws codebuild list-builds-for-project`).

### P0-5 — No cycle detection in dependency evaluator

- **Gap.** `src/orchestrator/evaluate.py:8-69` re-evaluates each invocation. A cyclic dep (`A→B→A`) populates neither `ready` nor `failed_deps`, so `handler.py:53` (`while True: ready, failed_deps, waiting = evaluate_orders(...); if not failed_deps: break`) terminates but the orders loop forever across orchestrator invocations.
- **Fix.** Add a DFS cycle check in `src/init_job/validate.py` that runs at submission time. Return `[f"cyclic dependency involving {cycle_path}"]`. Fail fast, not at run time.
- **Verification.**
  - Unit: add `tests/unit/test_validate_cycles.py::test_direct_cycle` (A→B, B→A), `::test_indirect_cycle` (A→B→C→A), `::test_self_loop` (A→A), `::test_diamond_no_cycle` (A→B, A→C, B→D, C→D).
  - Integration: submit a cyclic job via `POST /init`; expect HTTP 400 with the cycle path in the error body.

### P0-6 — Lock acquire condition lacks TTL check

- **Gap.** `src/common/dynamodb.py:248` uses `Attr("pk").not_exists() | Attr("status").eq("completed")`. A stale but unexpired lock blocks for up to the full `ttl_hours`. DynamoDB TTL cleanup is eventually consistent (up to 48h).
- **Fix.** Add an `OR ttl < :now` clause: `Attr("pk").not_exists() | Attr("status").eq("completed") | Attr("ttl").lt(Decimal(now))`. Pass `:now` as an ExpressionAttributeValue.
- **Verification.**
  - Unit: add `tests/unit/test_lock_acquire.py::test_expired_lock_is_stealable` — seed a lock row with `ttl = now - 60`, call `acquire_lock`, assert success.
  - Unit: `::test_active_lock_blocks` — seed with `ttl = now + 3600`, assert failure.
  - Unit: `::test_completed_lock_is_stealable` — seed with `status="completed"`, assert success.

### P0-7 — SSM order credentials stored plaintext in DynamoDB `env_dict`

- **Gap.** `src/common/models.py:213` defines `env_dict: Optional[Dict[str, str]]`; `src/ssm_config/insert.py` writes it unencrypted. Any operator with `dynamodb:GetItem` on the orders table reads every SSM order's secrets.
- **Fix.** Option A (smaller): stop persisting `env_dict` to the orders row altogether; it already flows through the dispatch `SendCommand` parameters (`src/orchestrator/dispatch.py:79-80`). Verify the DynamoDB copy is not read elsewhere, then delete the field from `OrderRecord`.
- **Verification.**
  - Unit: add `tests/unit/test_ssm_insert.py::test_env_dict_not_persisted` — invoke `insert_ssm_order`, read the item back, assert `"env_dict" not in item`.
  - Grep audit: `Grep("env_dict", glob="src/**/*.py")` — confirm only writers and the dispatch read path remain; no lingering consumers of the DynamoDB field.
  - Security: after deploy, `aws dynamodb scan --table orders --projection-expression env_dict` — should return zero populated values.

### P0-8 — Presign-expiry vs order-timeout validation missing

- **Gap.** `src/init_job/validate.py:16-38` does not compare `job.presign_expiry` (default 7200, `src/common/models.py:91`) to any `order.timeout`. A `presign_expiry=3600` with `order.timeout=14400` passes validation and silently fails at callback time.
- **Fix.** Add to `validate.py`:
  ```python
  max_timeout = max((o.timeout for o in orders), default=0)
  buffer = 300  # 5 min
  if job.presign_expiry < max_timeout + buffer:
      errors.append(f"presign_expiry ({job.presign_expiry}) must be >= max order timeout ({max_timeout}) + {buffer}s buffer")
  ```
- **Verification.**
  - Unit: add `tests/unit/test_validate_presign.py::test_presign_too_short` (expect error), `::test_presign_adequate` (expect pass), `::test_presign_exact_max_plus_buffer` (expect pass).
  - Integration: submit a job with `presign_expiry=600, order.timeout=1800`; expect HTTP 400.

### P0-9 — `use_lambda` backward-compat fallback is documented but not implemented

- **Gap.** `docs/ARCHITECTURE.md:387,394` shows `use_lambda=true → lambda, use_lambda=false → codebuild`. `src/orchestrator/dispatch.py:146` only reads `execution_target`, default `codebuild`.
- **Fix.** Remove the `use_lambda` references from `docs/ARCHITECTURE.md`. The feature was never implemented and the path forward is `execution_target`-only.
- **Verification.**
  - `Grep("use_lambda", glob="docs/**/*.md")` — expect zero matches after the fix.
  - `Grep("use_lambda", glob="src/**/*.py")` — expect zero matches (confirming the feature truly doesn't exist).

### P0-10 — Delete stale `TODO/sops-key-ssm-storage.md`

- **Gap.** The TODO describes work that exists: `src/common/sops.py:45-99`, `src/init_job/repackage.py:50-56`, `src/worker/run.py:25-34`, `src/orchestrator/dispatch.py:35-58`, `src/orchestrator/finalize.py:93-99`.
- **Fix.** `rm TODO/sops-key-ssm-storage.md`.
- **Verification.** `Glob("TODO/*.md")` — file should be absent. No test needed.

### P0-11 — `docs/REPO_STRUCTURE.md:33` references phantom `src/init_job/pr_comment.py`

- **Gap.** File does not exist; `CLAUDE.md:28` says PR comments are disabled.
- **Fix.** Remove the line from `docs/REPO_STRUCTURE.md`.
- **Verification.** `Grep("pr_comment", glob="docs/**/*.md")` — expect zero matches. `Glob("src/init_job/pr_comment.py")` — expect empty.

### P0-12 — PR comment flow shown in architecture doc but disabled in code

- **Gap.** `docs/ARCHITECTURE.md:142, 343, 388, 427` show init and finalize PR comments; `CLAUDE.md:28` says PR comments are disabled and the caller owns the lifecycle.
- **Fix.** Remove the PR comment steps from the mermaid diagrams in `docs/ARCHITECTURE.md`. Add a one-paragraph note: *"PR comments are the caller's responsibility. The engine exposes `src/common/vcs/` as a library for callers that need it, but does not post comments itself."*
- **Verification.**
  - `Grep("PR comment", glob="docs/**/*.md")` — should show only the new explanatory note, no mermaid steps.
  - Visual: render the mermaid diagrams and confirm "Init PR Comment" / "Final PR comment" nodes are gone.

### P0-13 — SSM SOPS key path drift (`/aws-exe-sys/` in docs vs `/exe-sys/` in code)

- **Gap.** `docs/ARCHITECTURE.md:101, 466, 572` show `/aws-exe-sys/sops-keys/…`. Code is internally consistent on `/exe-sys/sops-keys/…` (`src/common/sops.py:57`, `infra/02-deploy/lambdas.tf:8`, `infra/02-deploy/iam.tf:229`).
- **Fix.** Update `docs/ARCHITECTURE.md` to `/exe-sys/sops-keys/…` (code is authoritative; operators debugging will `aws ssm get-parameter` against the real path).
- **Verification.** `Grep("aws-exe-sys/sops-keys", glob="docs/**/*.md")` — expect zero matches after fix.

### P0 also-add (from RESEARCH4 §3.1) — Rename or remove `fetch_secret_values`

- **Gap A (RESEARCH4).** `src/common/code_source.py:33-43` derives the env var name from the Secrets Manager path, not the secret payload. A multi-field secret `{"username": "u", "password": "p"}` becomes `{"SECRET_NAME": '{"username":"u","password":"p"}'}` — forcing consumers to JSON-parse env vars in their scripts.
- **Fix.** Change `fetch_secret_values` to parse `SecretString` as JSON when possible: if it's a JSON dict, `result.update(parsed)`; if it's a plain string, use the path-derived key as today. Document the behavior in `docs/VARIABLES.md`.
- **Verification.**
  - Unit: `tests/unit/test_code_source.py::test_fetch_secret_values_json_dict_expands_to_multiple_keys`, `::test_fetch_secret_values_plain_string_uses_path_key`.
  - Integration: store a secret with a JSON payload and confirm the worker receives both env vars.

### P0 also-add (from RESEARCH4 §3.2) — `fetch_sops_key_ssm` has no recovery path

- **Gap B (RESEARCH4).** `src/common/sops.py:83-90` does not catch `ParameterNotFound`. A worker queued past the 2 h SOPS key TTL crashes with a misleading error.
- **Fix.** Catch `ClientError` / `ParameterNotFound` in `fetch_sops_key_ssm` and raise a domain-specific `SopsKeyExpired` exception. In `src/worker/run.py:25-34`, catch that exception and callback with `status="failed"`, `error="sops_key_expired"`. This converts a worker crash into a clean failed order.
- **Verification.**
  - Unit: `tests/unit/test_sops.py::test_fetch_sops_key_ssm_raises_domain_error_on_missing` — mock SSM to raise `ParameterNotFound`; assert `SopsKeyExpired` is raised.
  - Unit: `tests/unit/test_worker_run.py::test_worker_callbacks_failed_on_sops_key_expired` — patch `fetch_sops_key_ssm` to raise; assert `send_callback` called with `status="failed"`.

### P0 also-add (from RESEARCH4 §3.3) — Document base64/JSON contract for SSM credential values

- **Gap C (RESEARCH4).** `src/common/code_source.py:15-30` requires every SSM parameter to be a base64-encoded JSON dict. Not documented in `docs/VARIABLES.md`. `resolve_git_credentials` then takes the first value from the dict (insertion order). Silent footgun.
- **Fix.** Update `docs/VARIABLES.md:14` with the explicit contract: *"SSM values referenced by `git_token_location` or listed in `ssm_paths` must be base64-encoded JSON objects with string values. For `git_token_location`, the first value is used as the token (dict ordering is insertion order)."* Add a worked example.
- **Verification.**
  - `Grep("base64", glob="docs/VARIABLES.md")` — expect at least one match after the fix.
  - Also add a defensive `ValueError("SSM value at <path> is not a base64-encoded JSON dict")` at `code_source.py:24` instead of letting `binascii.Error` bubble up. Unit test: `test_fetch_ssm_values_raises_helpful_error_on_plain_string`.

---

## Phase P1 — Turn on the bootstrap seam

One architectural change. Everything in P2 and beyond rebases on it.

### P1-1 — Wire `bootstrap_handler.py` into all five Lambdas

- **Gap (RESEARCH3 §2, RESEARCH4 gap 1).** `src/bootstrap_handler.py` is dead code. `infra/02-deploy/lambdas.tf:22, 48, 72, 91, 111` all hardcode `src.<module>.handler.handler`. No `ENGINE_HANDLER`, `ENGINE_CODE_URL`, or `ENGINE_CODE_SSM_PATH` anywhere.
- **Fix.**
  1. Add `variable "engine_code_source"` in `infra/02-deploy/vars.tf` with shape `{ kind = "inline" | "ssm_url" | "presigned", value = string }`. `inline` = load from the baked image (current behavior); others = bootstrap seam.
  2. For each Lambda in `lambdas.tf`, add an env var `ENGINE_HANDLER = "src.<module>.handler"`, `ENGINE_HANDLER_FUNC = "handler"`, and `ENGINE_CODE_SSM_PATH` when `kind != "inline"`.
  3. Change `image_config.command` to `["src.bootstrap_handler.handler"]` when `kind != "inline"`; otherwise keep the current direct path.
  4. IAM: add `ssm:GetParameter` on `arn:aws:ssm:*:*:parameter/<engine_code_ssm_path>*` to each Lambda role when `kind != "inline"`.
  5. Add a SHA256 integrity check in `bootstrap_handler.py` (see P1-2 below) before making the seam live.
- **Verification.**
  - Unit: `tests/unit/test_bootstrap_handler.py` already exists; confirm it still passes.
  - Terraform: `tofu plan` with `kind = "inline"` → zero diff against the current deployment.
  - Terraform: `tofu plan` with `kind = "ssm_url"` → diff shows `command` and env-var changes.
  - Integration: deploy with `kind = "ssm_url"` pointing at a test tarball. Submit a job. Confirm via CloudWatch Logs that `bootstrap_handler._load_engine()` is called exactly once per cold start.
  - Regression: confirm `tests/smoke/test_deploy.sh` still passes.

### P1-2 — Integrity-verify the bootstrap tarball

- **Gap (RESEARCH3 §2.5).** No SHA, no signature. `bootstrap_handler.py:77` comment `# trusted tarball from our own S3` is load-bearing; whoever writes to the SSM path can RCE every Lambda.
- **Fix.**
  1. Require the SSM-stored URL JSON payload to include `{"url": "...", "sha256": "abc..."}`.
  2. In `bootstrap_handler._download_tarball()`, verify the downloaded bytes against the SHA before extracting.
  3. Raise and exit on mismatch; log the mismatch to stderr.
  4. Tighten IAM: `ssm:PutParameter` on the engine code path restricted to the deploy role only.
- **Verification.**
  - Unit: `tests/unit/test_bootstrap_handler.py::test_sha_mismatch_raises` — seed a tarball, compute the wrong SHA, assert `SystemExit` or specific exception.
  - Unit: `::test_sha_match_loads` — happy path.
  - IAM: `aws iam simulate-principal-policy` with a non-deploy role and `ssm:PutParameter` on the engine path → expect DENY.

---

## Phase P2 — Registries for pluggable surfaces

Four registries — credentials, VCS, code sources, execution targets. Each converts a hardcoded `if/elif` into a protocol + a dict. Each is independent; do them in parallel if desired.

### P2-1 — Credential provider registry

- **Gap (RESEARCH3 §4, RESEARCH4 gap 3).** `_strip_location_prefix` at `src/common/code_source.py:46-54` handles only `aws:::ssm:`. `aws:::secretd:` is documented but never parsed. Two different fetch models (prefix URI vs `ssm_paths`/`secret_manager_paths`) coexist.
- **Fix.**
  1. Define a protocol in `src/common/credentials/base.py`:
     ```python
     class CredentialProvider(Protocol):
         scheme: str  # e.g. "aws_ssm", "aws_secretsmanager"
         def fetch(self, path: str, region: Optional[str] = None) -> Dict[str, str]: ...
     ```
  2. Implement `AwsSsmProvider`, `AwsSecretsManagerProvider` in `src/common/credentials/`.
  3. Registry in `src/common/credentials/registry.py`: `PROVIDERS: Dict[str, CredentialProvider] = {}` with `register_provider(p: CredentialProvider)`.
  4. Parse `vendor:::scheme:path` once in `resolve_location(location) -> (provider, path)`.
  5. Both `resolve_git_credentials` and per-order `ssm_paths`/`secret_manager_paths` routes through the registry.
- **Verification.**
  - Unit: `tests/unit/test_credentials_registry.py::test_register_and_fetch`, `::test_aws_ssm_prefix_resolves`, `::test_aws_secretsmanager_prefix_resolves`, `::test_unknown_scheme_raises`.
  - Unit: `::test_third_party_provider_registration` — register a fake `vault` provider, confirm `fetch("vault:::kv/foo")` dispatches to it.
  - Integration: submit a job using `aws:::secretd:...` location; confirm it resolves (this would 404 today).
  - Regression: existing tests using `aws:::ssm:` continue to pass.

### P2-2 — VCS provider registry (clone + comment + metadata)

- **Gap (RESEARCH3 §3, RESEARCH4 gap 2).** `src/common/vcs/helper.py:14-17` is a bare dict. `src/common/code_source.py:109, 128, 156` hardcode `github.com`. Clone is split from the VCS abstraction. `VcsHelper` only wraps comments.
- **Fix.**
  1. Extend `src/common/vcs/base.py::VcsProvider` ABC with methods: `clone(repo: str, commit: str, dest: Path, token: Optional[str]) -> None`, `clone_ssh(repo: str, commit: str, dest: Path, ssh_key: str) -> None`, `get_clone_url(repo: str, token: Optional[str]) -> str`.
  2. Move `src/common/code_source.py:90-151` clone logic into `GitHubProvider.clone()`. Parameterize the host.
  3. Add `register_provider(name: str, cls: Type[VcsProvider])` public API.
  4. `clone_repo()` in `code_source.py` looks up the provider by `job.git_provider` (default `"github"`) and dispatches.
- **Verification.**
  - Unit: `tests/unit/test_vcs_github.py::test_clone_https_token_url`, `::test_clone_https_anonymous`, `::test_clone_ssh`.
  - Unit: `tests/unit/test_vcs_registry.py::test_register_bitbucket_stub` — register a stub Bitbucket provider, set `job.git_provider="bitbucket"`, call `clone_repo`, assert Bitbucket stub's `clone()` was called.
  - Regression: existing GitHub clones continue to work against a mock git server.

### P2-3 — Code source protocol

- **Gap (RESEARCH3 §5, RESEARCH4 gap 4).** `src/common/code_source.py:184-212` is an `if/elif` over `s3_location` / `git_repo`. No `CodeSource` ABC. Adding a new source edits 4 files.
- **Fix.**
  1. Define `CodeSource` protocol in `src/common/code_sources/base.py` with `fetch(order, job, dest_dir) -> Path`.
  2. Implementations: `GitCodeSource`, `S3CodeSource`, `CommandsOnlyCodeSource` in `src/common/code_sources/`.
  3. Registry with `register_code_source()`.
  4. `group_git_orders` collapses to `for order in orders: kind = detect_kind(order); sources[kind].fetch(...)`.
  5. Both `init_job/repackage.py` and `ssm_config/repackage.py` go through the registry. Delete the Phase-1/Phase-2/Phase-3 split.
- **Verification.**
  - Unit: `tests/unit/test_code_sources.py::test_git_source_clones`, `::test_s3_source_downloads`, `::test_commands_only_creates_empty_dir`.
  - Unit: `::test_register_third_party_source` — register a stub `http_tarball` source; confirm dispatch works.
  - Regression: full existing repackage tests (both init_job and ssm_config) continue to pass.

### P2-4 — Execution target registry

- **Gap (RESEARCH3 §6, RESEARCH4 gap 5).** `src/orchestrator/dispatch.py:146-154` is a hardcoded `if/elif/else`.
- **Fix.**
  1. Define `ExecutionTarget` protocol in `src/orchestrator/targets/base.py`:
     ```python
     class ExecutionTarget(Protocol):
         name: str
         def dispatch(self, order: dict, run_id: str, internal_bucket: str) -> str: ...
     ```
  2. Implementations: `LambdaTarget`, `CodeBuildTarget`, `SsmTarget` in `src/orchestrator/targets/`.
  3. Registry with `register_target()`. Seed with the three defaults at module import.
  4. `src/common/statuses.py:11` reads `EXECUTION_TARGETS` from `TARGETS.keys()` instead of the frozenset.
  5. `dispatch.py:146-154` collapses to `execution_id = TARGETS[execution_target].dispatch(order, run_id, internal_bucket)`.
- **Verification.**
  - Unit: `tests/unit/test_dispatch_targets.py::test_lambda_target`, `::test_codebuild_target`, `::test_ssm_target` — mock each AWS client, assert the right API call.
  - Unit: `::test_register_ecs_fargate_stub` — register a fake target, dispatch an order with that `execution_target`, assert stub was called.
  - Unit: `::test_unknown_target_raises` — expect a clear error message naming the unknown target.
  - Regression: existing dispatch tests continue to pass.

---

## Phase P3 — Runtime hardening

Items that RESEARCH2 flagged as runtime fragility. Several are already covered in P0 (lock TTL, pre-dispatch status, cycle detection). P3 picks up the remaining items.

### P3-1 — Worker callback fallback

- **Gap (RESEARCH3 §11 item 17, RESEARCH4 §2 row).** `src/worker/callback.py:48-49` returns `False` on failure; `src/worker/run.py` ignores it. Worker has zero DynamoDB write permissions (`infra/02-deploy/iam.tf:209-232`).
- **Fix (canonical path).** Grant the worker narrow `dynamodb:UpdateItem` on the orders table, limited to `status` and `last_update` attributes via an IAM condition. In `callback.py`, on exhausted retries, fall back to a direct `UpdateItem` call writing `status="failed"`, `error="callback_failed"`. Log both the S3 failure and the DynamoDB fallback.
- **Verification.**
  - Unit: `tests/unit/test_callback_fallback.py::test_s3_callback_failure_triggers_dynamodb_fallback` — mock S3 PUT to 500, assert `UpdateItem` called with `status="failed"`.
  - Unit: `::test_s3_callback_success_skips_dynamodb` — confirm DynamoDB is not called on success.
  - IAM: `aws iam simulate-principal-policy` confirming `UpdateItem` on the orders table succeeds, `PutItem` is denied.

### P3-2 — Watchdog jitter + MaxAttempts bound

- **Gap (RESEARCH3 §11 item 18).** `infra/02-deploy/step_functions.tf:26-30` has `Seconds: 60` literal, no jitter, no upper bound.
- **Fix.**
  1. Replace the `Wait` state with a small Lambda-backed wait that returns `60 + random.randint(-10, 10)`.
  2. Add a `MaxAttempts` guard computed from `order.timeout / 60 + safety_margin` — bounded by something like `2 * order.timeout`.
  3. When the cap is hit, write `status="timed_out_watchdog_cap"` so it is distinguishable from the regular timeout path.
- **Verification.**
  - Unit: `tests/unit/test_watchdog.py::test_jitter_within_range` — call the wait-computation function 100 times, assert all in `[50, 70]`.
  - Integration: trigger an artificially long-running order and confirm the Step Function terminates cleanly at the cap, not after 1 year.

### P3-3 — SOPS key TTL coordination

- **Gap (RESEARCH3 §9 SOPS row, RESEARCH4 §3.2).** `src/common/sops.py:49` default `ttl_hours=2`, no caller overrides, no relationship to `order.timeout` / `job.job_timeout`.
- **Fix.** In `src/init_job/repackage.py:50-56` (where `store_sops_key_ssm` is called), compute `ttl_hours = max(job.job_timeout, max(order.timeout for order in orders)) / 3600 + 1` (1-hour safety margin). Pass explicitly.
- **Verification.**
  - Unit: `tests/unit/test_sops_ttl.py::test_ttl_scales_with_longest_order` — a job with a 4 h order should store the SOPS key with `ttl_hours >= 5`.
  - Integration: submit a long-running job, then `aws ssm describe-parameters` confirming the expiration policy matches.

---

## Phase P4 — Framework polish

### P4-1 — Event sink abstraction

- **Gap (RESEARCH3 §7).** `src/common/dynamodb.py:144-176`'s `put_event` is the single chokepoint; no `EventSink` protocol.
- **Fix.** Introduce `src/common/events/sinks.py` with an `EventSink` protocol (`emit(event: dict) -> None`), a default `DynamoDbEventSink`, and a `CompositeEventSink` for mirroring. Register via env var `AWS_EXE_SYS_EVENT_SINKS` (comma-separated).
- **Verification.**
  - Unit: `::test_dynamodb_sink_default`, `::test_composite_sink_mirrors`, `::test_register_third_party_sink`.

### P4-2 — Installable package name

- **Gap (RESEARCH3 §11 item 20).** Module paths start with `src.`, which is a deployment hack.
- **Fix.** Move code to `aws_exe_sys/…`, keep `src.*` as a compat shim. Add `pyproject.toml` with the new name.
- **Verification.** `python -c "import aws_exe_sys"` works; `python -c "from src.orchestrator.handler import handler"` still works (via compat shim).

### P4-3 — Versioned result schema

- **Gap (RESEARCH3 §11 item 21).** No schema version on `result.json`.
- **Fix.** Add `schema_version: "v1"` to every `result.json` and `put_event` call. Define the schema in `src/common/schemas.py` as a Pydantic model.
- **Verification.**
  - Unit: `::test_result_v1_round_trip`, `::test_result_missing_version_rejected`.

### P4-4 — CI drift test

- **Gap (RESEARCH3 §11 item 22).** The doc-code drift pile will regrow without a guard.
- **Fix.** Add `tests/unit/test_contract_drift.py` (started in P0-2) that regex-extracts claims from `CONTRACT.md` / `CLAUDE.md` / `docs/**.md` and asserts each claim still holds in code:
  - Event TTL value
  - SK format string
  - Lock TTL
  - All documented env-var names exist in code
  - All documented `execution_target` values exist in `EXECUTION_TARGETS`
  - SSM SOPS key path prefix
- **Verification.** Run the test against the current tree (post P0 fixes); it should pass. Deliberately revert one P0 fix; the test should fail with a clear message naming the drift.

---

## Verification harness — cross-cutting

Beyond per-item verifications, three cross-cutting checks prove the plan actually converged:

1. **Full unit suite.** `cd tests && python -m pytest unit/ -v` — zero failures, zero skips (other than explicitly marked slow tests).
2. **Smoke test.** `bash tests/smoke/test_deploy.sh` — green against a test-account deploy.
3. **End-to-end run.** Submit a multi-order job via `POST /init` with:
   - One Lambda order (short, `execution_target=lambda`)
   - One CodeBuild order (long, depends on the Lambda order)
   - One SSM order depending on CodeBuild
   - One intentionally cyclic order pair (expect 400 at submission — validates P0-5)
   - A `presign_expiry` too short (expect 400 at submission — validates P0-8)

   Then verify:
   - All non-cyclic orders reach `status=completed`.
   - Events in `order_events` survive >1h (validates P0-1).
   - `sk` format in `order_events` matches the code literal (validates P0-2).
   - Lock row has non-empty `flow_id` / `trace_id` (validates P0-3).
   - No duplicate CodeBuild builds (validates P0-4).
   - No plaintext secrets in the orders row's `env_dict` (validates P0-7).

---

## Execution sequencing

| Phase | Parallelizable? | Blocker for next phase? |
|---|---|---|
| P0 (13 + 3 items) | Yes — all independent | No — can start P1/P2 in parallel with the slower P0 items |
| P1 (bootstrap seam + integrity check) | No — P1-1 and P1-2 are sequential | Yes — P2 should rebase on it |
| P2 (four registries) | Yes — independent of each other | No |
| P3 (hardening) | Yes — three independent items | No |
| P4 (polish) | Yes | No |

**Recommended starting point:** knock out P0-1, P0-2, P0-3, P0-10, P0-11, P0-12, P0-13 in a single PR (they are all one-liners or doc edits). Then P0-4 through P0-9 and the RESEARCH4 additions each as their own PR, since they require real test work. P1 is one big PR. Each P2 registry is its own PR.

---

## Appendix — anchor index (from RESEARCH4 §4)

| # | Bug / drift | File:line |
|---|---|---|
| 1 | Event TTL 15 min → 90 days | `src/common/dynamodb.py:170` |
| 2 | Event SK format | `src/common/dynamodb.py:162` vs `CONTRACT.md:101` |
| 3 | Lock with empty `flow_id`/`trace_id` | `src/orchestrator/handler.py:128` |
| 4 | Dispatch before status update | `src/orchestrator/dispatch.py:149-154, :160-169` |
| 5 | Cycle detection missing | `src/orchestrator/evaluate.py:8-69` → add to `src/init_job/validate.py` |
| 6 | Lock acquire lacks TTL check | `src/common/dynamodb.py:248` |
| 7 | SSM `env_dict` plaintext | `src/common/models.py:213`, `src/ssm_config/insert.py` |
| 8 | Presign vs timeout validation | `src/init_job/validate.py:16-38` |
| 9 | `use_lambda` doc drift | `docs/ARCHITECTURE.md:387, 394` |
| 10 | Stale TODO | `TODO/sops-key-ssm-storage.md` |
| 11 | Phantom `pr_comment.py` reference | `docs/REPO_STRUCTURE.md:33` |
| 12 | PR comment flow in diagrams | `docs/ARCHITECTURE.md:142, 343, 388, 427` |
| 13 | SSM SOPS path prefix drift | `docs/ARCHITECTURE.md:101, 466, 572` |
| A | `fetch_secret_values` multi-field | `src/common/code_source.py:33-43` |
| B | `fetch_sops_key_ssm` no recovery | `src/common/sops.py:83-90` |
| C | Undocumented base64/JSON contract | `src/common/code_source.py:15-30`, `docs/VARIABLES.md:14` |
| Seam | Bootstrap handler not wired | `infra/02-deploy/lambdas.tf:22, 48, 72, 91, 111` |
| VCS | Hardcoded `github.com` | `src/common/code_source.py:109, 128, 156` |
| Creds | `aws:::secretd:` not parsed | `src/common/code_source.py:46-54` |
| Sources | Hardcoded if/elif | `src/common/code_source.py:184-212` |
| Targets | Hardcoded if/elif | `src/orchestrator/dispatch.py:146-154` |

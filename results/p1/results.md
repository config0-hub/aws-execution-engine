# aws-exe-sys — P1 bootstrap seam (results)

**Date:** 2026-04-10
**Plan:** `src/delegated-execution-components/aws-execution-engine/plan-to-fix-gaps-04.09.2026.md` (Phase P1)
**Scope:** P1-1 (bootstrap seam wiring) + P1-2 (SHA256 integrity verification). Standalone pluggable library — verified via library gate, not live deploy.
**Workflow:** `/team:implement-simple` with 1 worker + deployer. P0 was a previous session (see `results.md` in this directory).

## Outcome

**PASS.** All P1 items shipped. 399/399 unit tests green in Woodpecker CI pipeline #36.

## P1 items — status

| ID | Item | Status |
|---|---|---|
| P1-1 | Wire `bootstrap_handler` into 5 Lambdas via conditional `image_config.command` + env vars + IAM | done — `engine_code_source` Terraform variable with `kind="inline"` default, `locals` pattern (`engine_bootstrap_env={}`, `engine_code_read_statements=[]`) guarantees zero-diff when inline |
| P1-2 | SHA256 integrity verification in `bootstrap_handler.py` | done — strict `{url, sha256}` JSON payload shape, greenfield (no legacy plain-string support), `BootstrapIntegrityError` raised on shape violations or SHA mismatch |

The Terraform variable is scoped to `{kind: "inline" | "ssm_url", value: string}` (dropped the `"presigned"` kind from the plan — the event-payload path `event["engine_code"]` handles that case without Terraform plumbing).

## Code changes

| File | Change |
|---|---|
| `src/bootstrap_handler.py` | Rewritten from 100 → 192 LoC. Strict shape everywhere (`event.engine_code = {url, sha256}` dict — plain string explicitly rejected; env vars `ENGINE_CODE_URL` + `ENGINE_CODE_SHA256` both required; SSM parameter base64-encoded JSON `{url, sha256}` — plain base64 URL rejected). Download → SHA256 verify → extract, verify happens before extraction. `_loaded` cache preserved for warm invocations. |
| `tests/unit/test_bootstrap_handler.py` | 13 existing tests updated to strict shape, 7 new tests added |
| `infra/02-deploy/variables.tf` | New `engine_code_source` object variable with 3 validations (`kind` enum, value-required-when-ssm_url) |
| `infra/02-deploy/lambdas.tf` | New `local.engine_bootstrap_env = kind == "ssm_url" ? {...} : {}` — empty map when inline. Each of 5 Lambdas wrapped in `merge(base, {lambda_specific}, local.engine_bootstrap_env, kind == "ssm_url" ? {ENGINE_HANDLER=...} : {})`. `image_config.command` wrapped in `kind == "inline" ? [legacy] : [bootstrap]` ternary. |
| `infra/02-deploy/iam.tf` | New `local.engine_code_read_statements = kind == "ssm_url" ? [...] : []`. Each of 5 Lambda role policies uses `concat(base, local.engine_code_read_statements)` — empty list when inline. No Lambda role has `ssm:PutParameter` on the engine code path (never did); deploy role is external to this Terraform module. |
| `infra/02-deploy/P1_VERIFICATION.md` | **New file.** Focused-module mathematical proof of the zero-diff-when-inline invariant + deployer guidance. |

## Tests — 7 new

| Test | Purpose |
|---|---|
| `test_sha_mismatch_raises` | Seed tarball on disk, wrong SHA → `BootstrapIntegrityError` raised |
| `test_sha_match_loads` | Happy path — correct SHA, module imports cleanly |
| `test_ssm_payload_json_shape` | SSM returns `{url, sha256}` JSON — both fields extracted |
| `test_ssm_payload_legacy_rejected` | SSM returns plain base64 URL (no JSON wrapper) → clear error, no silent fallback |
| `test_ssm_payload_json_missing_sha` | SSM returns `{url}` without `sha256` → clear error |
| `test_event_plain_string_rejected` | Event payload `engine_code` as plain string → clear error |
| `test_env_vars_require_both_url_and_sha` | Env var fallback requires both `ENGINE_CODE_URL` and `ENGINE_CODE_SHA256` or neither |

Bootstrap tests total: **20** (7 new + 13 updated). Full engine unit suite: **399 passing** (392 P0 baseline + 7 P1).

## Zero-diff invariant proof

`infra/02-deploy/P1_VERIFICATION.md` contains the mathematical proof, reproduced here:

| Load-bearing identity | Terraform effect |
|---|---|
| `concat(L, []) ≡ L` | IAM policy `Statement` byte-identical after `jsonencode` |
| `merge(M, {}, {}) ≡ M` | Lambda `environment.variables` byte-identical |
| `kind == "inline" ? L : R ≡ L` | `image_config.command` byte-identical |

Terraform compares attribute *values* (not source expressions), so `tofu plan` against an inline-deployed stack produces **zero diff**. Consumers (ConfigZero, etc.) running their own infrastructure will see this when they pull the new library version with `engine_code_source` left at default.

Focused-module test output (verified by backend-worker):

```
kind = "inline":
  command_match  = true
  env_match      = true
  policies_match = true

kind = "ssm_url":
  command_match  = false    → ["src.bootstrap_handler.handler"]
  env_match      = false    + ENGINE_HANDLER, ENGINE_HANDLER_FUNC, ENGINE_CODE_SSM_PATH
  policies_match = false    + ssm:GetParameter on parameter${value}*
```

## Library verification gate

Per `feedback_library_vs_service_verification.md` (memory saved this session): aws-execution-engine is a standalone pluggable library, not a hosted service. Verification is:

1. ✅ Unit tests pass locally in Docker
2. ✅ Commit + `tools/sync-forgejo.sh aws-execution-engine`
3. ✅ Woodpecker pipeline unit-tests step green

No live `tofu apply/plan` against real AWS. Consumers run their own infra when they upgrade the library version.

## Deploy evidence

| Item | Value |
|---|---|
| Local commit SHA | `20516044fcd7964bb378f09731740ef21e5243ee` |
| Local commit subject | `feat(aws-exe-sys): P1 bootstrap seam wiring + SHA256 integrity verification` |
| Forgejo commit SHA | `c6f8fe8f49d9e39de66af4aaa2df3e067e386ec9` |
| Forgejo repo | `forgejo_admin/aws-execution-engine` |
| Woodpecker pipeline | #36 |
| Pipeline URL | `http://woodpecker/repos/forgejo_admin/aws-execution-engine/pipeline/36` |
| Pipeline status | **success** |
| Total tests | `============================= 399 passed in 7.08s ==============================` |

All 7 new bootstrap integrity tests verified BY NAME in the pipeline log.

## Deviations and notes

- **State bucket residue (L1).** Before the library-gate pivot landed, the deployer executed `infra/00-bootstrap && tofu apply` and created `s3://config0-xe-state` in dev101 us-east-1 (bucket + versioning + SSE + public-access-block). Zero data, zero ongoing cost. The local `00-bootstrap/terraform.tfstate` and the real bucket are in sync. Left in place (not destroyed) because destroying would create new drift, and future consumers/contributors who want a real deploy will need it anyway.
- **Pre-existing region drift** (`backend.tf` says us-east-1, some consumer deploys are in ap-northeast-1) is out of scope for P1. It is a consumer-side concern, not a library-side concern. Should be tracked as a separate follow-up if the team wants library `backend.tf` updated.
- **PutParameter tightening (plan P1-2 step 4)** — no Lambda role had `ssm:PutParameter` on the engine code path to begin with, so the "tighten to deploy role only" is a no-op in this Terraform stack. The deploy role is external (GitHub Actions OIDC or similar), outside the 02-deploy module. Documented in `P1_VERIFICATION.md`.
- **Not pushed to GitHub origin.** Commit `20516044` is on local `main` only. Separate user decision needed to push to origin.
- **"presigned" kind dropped from the Terraform variable.** Plan §P1-1 step 1 mentioned three kinds; only `inline` and `ssm_url` are implemented. The presigned path already exists inside `bootstrap_handler` via `event["engine_code"]` (the event payload passes the URL + SHA at invoke time) and does not need Terraform plumbing.

## What's NOT in this run

Still deferred to future `/team:implement` runs:
- **P2** — four pluggable registries (credentials, VCS, code sources, execution targets) — now rebases cleanly on the bootstrap seam
- **P3** — runtime hardening (worker callback fallback, watchdog jitter, SOPS TTL coordination)
- **P4** — framework polish (event sinks, installable package, versioned result schema)

The bootstrap seam is **wired but not activated** — `kind="inline"` default means the next consumer deploy is a no-op. A consumer that wants to start using the seam sets `engine_code_source={kind="ssm_url", value="/exe-sys/engine-code"}` and provides the SSM parameter contents themselves.
